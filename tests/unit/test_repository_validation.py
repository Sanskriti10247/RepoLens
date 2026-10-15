import pytest

from repolens.repository.validation import (
    ValidationError,
    is_full_sha,
    parse_repository_url,
    validate_ref,
)


class TestRepositoryUrls:
    @pytest.mark.parametrize(
        ("url", "owner", "name"),
        [
            ("https://github.com/pallets/flask", "pallets", "flask"),
            ("https://github.com/pallets/flask.git", "pallets", "flask"),
            ("https://github.com/pallets/flask/", "pallets", "flask"),
            ("https://GitHub.com/Pallets/Flask", "Pallets", "Flask"),
            ("https://github.com/a-b/c.d_e-f", "a-b", "c.d_e-f"),
        ],
    )
    def test_accepts_github_repositories(self, url: str, owner: str, name: str) -> None:
        locator = parse_repository_url(url)
        assert (locator.provider, locator.owner, locator.name) == ("github", owner, name)
        assert locator.clone_url == f"https://github.com/{owner}/{name}.git"

    def test_canonical_url_is_case_insensitive(self) -> None:
        a = parse_repository_url("https://github.com/Pallets/Flask.git")
        b = parse_repository_url("https://github.com/pallets/flask")
        assert a.canonical_url == b.canonical_url == "https://github.com/pallets/flask"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "github.com/a/b",
            "http://github.com/a/b",
            "ssh://git@github.com/a/b.git",
            "git@github.com:a/b.git",
            "git://github.com/a/b",
            "file:///etc/passwd",
            "ext::sh -c touch% /tmp/pwned",
            "https://user:pass@github.com/a/b",
            "https://token@github.com/a/b",
            "https://github.com:443/a/b",
            "https://github.com.evil.com/a/b",
            "https://evil.com/github.com/a/b",
            "https://gitlab.com/a/b",
            "https://127.0.0.1/a/b",
            "https://[::1]/a/b",
            "https://169.254.169.254/latest/meta-data",
            "https://github.com/a",
            "https://github.com/a/b/c",
            "https://github.com/a/b/tree/main",
            "https://github.com/a/b?x=1",
            "https://github.com/a/b#frag",
            "https://github.com/a/b?",
            "https://github.com/../b",
            "https://github.com/a/..",
            "https://github.com/a/.hidden",
            "https://github.com/-a/b",
            "https://github.com/a%2Fb/c",
            "https://github.com/a\\b/c",
            "https://github.com/a/b\n",
            "https://github.com/a/b c",
            "https://g\u0456thub.com/a/b",  # Cyrillic i (homoglyph)
            "https://github.com/" + "a" * 40 + "/b",
            "https://github.com/a/" + "b" * 101,
        ],
    )
    def test_rejects_unsafe_or_unsupported_urls(self, url: str) -> None:
        with pytest.raises(ValidationError):
            parse_repository_url(url)


class TestRefs:
    @pytest.mark.parametrize(
        "ref",
        [
            "main",
            "feature/login-form",
            "v1.2.3",
            "release-2024.01",
            "HEAD",
            "abc1234",
            "0123456789abcdef0123456789abcdef01234567",
            "a" * 64,
        ],
    )
    def test_accepts_valid_refs(self, ref: str) -> None:
        assert validate_ref(ref) == ref

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "-h",
            "--upload-pack=touch /tmp/x",
            "--output=/etc/passwd",
            "a..b",
            "a b",
            "a~1",
            "a^",
            "a:b",
            "a?",
            "a*",
            "a[b",
            "a\\b",
            "a@{1}",
            "@",
            "/a",
            "a/",
            "a//b",
            "a.",
            ".a",
            "a/.b",
            "a.lock",
            "a/b.lock/c",
            "a\x00b",
            "a\nb",
            "a\x7fb",
            "réf",
            "x" * 256,
        ],
    )
    def test_rejects_invalid_refs(self, ref: str) -> None:
        with pytest.raises(ValidationError):
            validate_ref(ref)

    def test_full_sha_detection(self) -> None:
        assert is_full_sha("a" * 40)
        assert is_full_sha("b" * 64)
        assert not is_full_sha("a" * 39)
        assert not is_full_sha("A" * 40)
