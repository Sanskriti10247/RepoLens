"""Supabase access-token verification against a JWKS served from a local HTTP server."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repolens.api.app import create_app
from repolens.api.auth import TokenVerifier
from repolens.api.errors import ApiError
from repolens.config import Settings

pytestmark = pytest.mark.security

LEGACY_SECRET = "legacy-hs256-secret-for-tests-only-0123456789"
USER = uuid.UUID("2f6f3b0e-8f0c-4a8f-9d43-5d3b9b1f0a11")


class _Jwks:
    """A rotating key set: ``keys`` is what the endpoint serves right now."""

    def __init__(self) -> None:
        self.keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.fetches = 0

    def add(self, kid: str) -> ec.EllipticCurvePrivateKey:
        self.keys[kid] = ec.generate_private_key(ec.SECP256R1())
        return self.keys[kid]

    def document(self) -> dict[str, Any]:
        self.fetches += 1
        keys = []
        for kid, key in self.keys.items():
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
            keys.append({**jwk, "kid": kid, "alg": "ES256", "use": "sig"})
        return {"keys": keys}


@pytest.fixture
def jwks() -> Iterator[tuple[_Jwks, str]]:
    state = _Jwks()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/auth/v1/.well-known/jwks.json":
                self.send_error(404)
                return
            body = json.dumps(state.document()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _claims(base: str, **overrides: Any) -> dict[str, Any]:
    claims = {
        "sub": str(USER),
        "aud": "authenticated",
        "iss": f"{base}/auth/v1",
        "exp": int(time.time()) + 600,
        "role": "authenticated",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _es256(key: ec.EllipticCurvePrivateKey, kid: str, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def _rejected(verifier: TokenVerifier, token: str) -> None:
    with pytest.raises(ApiError) as caught:
        verifier.user_id(token)
    assert caught.value.status_code == 401


def test_valid_token_yields_user(jwks: tuple[_Jwks, str]) -> None:
    state, base = jwks
    key = state.add("k1")
    verifier = TokenVerifier(base)
    assert verifier.user_id(_es256(key, "k1", _claims(base))) == USER
    assert verifier.user_id(_es256(key, "k1", _claims(base))) == USER
    assert state.fetches == 1  # cached


def test_expired_wrong_issuer_audience_or_missing_claims(jwks: tuple[_Jwks, str]) -> None:
    state, base = jwks
    key = state.add("k1")
    verifier = TokenVerifier(base)
    _rejected(verifier, _es256(key, "k1", _claims(base, exp=int(time.time()) - 60)))
    _rejected(verifier, _es256(key, "k1", _claims(base, iss="https://evil.example/auth/v1")))
    _rejected(verifier, _es256(key, "k1", _claims(base, aud="anon")))
    _rejected(verifier, _es256(key, "k1", _claims(base, exp=None)))
    _rejected(verifier, _es256(key, "k1", _claims(base, sub="not-a-uuid")))
    _rejected(verifier, "not.a.jwt")


def test_signature_from_another_key_is_rejected(jwks: tuple[_Jwks, str]) -> None:
    state, base = jwks
    state.add("k1")
    forged = ec.generate_private_key(ec.SECP256R1())
    _rejected(TokenVerifier(base), _es256(forged, "k1", _claims(base)))


def test_unknown_kid_refetches_once_for_rotation(jwks: tuple[_Jwks, str]) -> None:
    state, base = jwks
    old = state.add("old")
    verifier = TokenVerifier(base)
    verifier.jwks.cooldown_duration = 0  # production waits 30 s between forced refetches
    assert verifier.user_id(_es256(old, "old", _claims(base))) == USER
    new = state.add("new")  # rotated after the verifier cached the set
    assert verifier.user_id(_es256(new, "new", _claims(base))) == USER
    assert state.fetches == 2
    stranger = ec.generate_private_key(ec.SECP256R1())
    _rejected(verifier, _es256(stranger, "never-published", _claims(base)))


def test_hs256_only_with_legacy_secret(jwks: tuple[_Jwks, str]) -> None:
    _, base = jwks
    token = jwt.encode(_claims(base), LEGACY_SECRET, algorithm="HS256")
    assert TokenVerifier(base, LEGACY_SECRET).user_id(token) == USER
    _rejected(TokenVerifier(base), token)
    wrong = jwt.encode(_claims(base), LEGACY_SECRET + "x", algorithm="HS256")
    _rejected(TokenVerifier(base, LEGACY_SECRET), wrong)
    unsigned = jwt.encode(_claims(base), None, algorithm="none")
    _rejected(TokenVerifier(base, LEGACY_SECRET), unsigned)


def test_every_route_but_health_requires_a_user(jwks: tuple[_Jwks, str]) -> None:
    _, base = jwks
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        supabase_url=base,
        api_token=SecretStr("operator-token"),
    )
    zero = "00000000-0000-0000-0000-000000000000"
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
        assert client.get("/api/health").status_code == 200
        for path, operations in paths.items():
            if path in {"/api/health", "/api/ready"}:
                continue
            url = path.replace("{", "").replace("}", "")
            for name in ("repository_id", "analysis_id", "impact_id", "plan_id", "run_id"):
                url = url.replace(name, zero)
            url = url.replace("job_id", zero).replace("symbol_id", "1").replace("file_id", "1")
            for method in operations:
                for headers in ({}, {"Authorization": "Bearer operator-token"}):
                    response = client.request(method.upper(), url, headers=headers, json={})
                    assert response.status_code == 401, (method, url, headers, response.text)
                    assert response.json()["error"]["code"] == "unauthorized"
