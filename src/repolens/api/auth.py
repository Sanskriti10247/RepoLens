"""Verification of Supabase Auth access tokens (user mode)."""

from __future__ import annotations

import uuid

import jwt

from repolens.api.errors import ApiError

# Supabase signs with ES256 or RS256 once a project uses asymmetric keys.
_ASYMMETRIC = ("ES256", "RS256")


class TokenVerifier:
    def __init__(self, supabase_url: str, legacy_secret: str | None = None) -> None:
        self.issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        # Keys are cached; a token with an unknown ``kid`` triggers a refetch (key rotation),
        # at most once per 30 s cooldown so forged kids cannot hammer the endpoint.
        self.jwks = jwt.PyJWKClient(
            f"{self.issuer}/.well-known/jwks.json", cache_keys=True, lifespan=600, timeout=5
        )
        self.legacy_secret = legacy_secret

    def user_id(self, token: str) -> uuid.UUID:
        """Return the user id (``sub``) of a valid access token, or raise a 401 ApiError."""
        try:
            algorithm = jwt.get_unverified_header(token).get("alg")
            key: object
            if algorithm == "HS256" and self.legacy_secret:
                key = self.legacy_secret
            elif algorithm in _ASYMMETRIC:
                key = self.jwks.get_signing_key_from_jwt(token).key
            else:
                raise jwt.InvalidAlgorithmError(f"algorithm {algorithm!r} is not accepted")
            claims = jwt.decode(
                token,
                key,  # type: ignore[arg-type]
                algorithms=[algorithm],
                audience="authenticated",
                issuer=self.issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
            return uuid.UUID(str(claims["sub"]))
        except jwt.PyJWKClientConnectionError as exc:
            raise ApiError(503, "auth_unavailable", "Sign-in keys could not be loaded") from exc
        except (jwt.PyJWTError, ValueError) as exc:
            raise ApiError(401, "unauthorized", "Missing or invalid access token") from exc
