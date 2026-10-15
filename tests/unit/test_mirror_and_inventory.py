"""Mirror fetching, revision resolution and inventory against real local Git repositories."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from repolens.domain.enums import FileMode, FileRole
from repolens.repository.git import GitPolicy, GitRunner
from repolens.repository.inventory import (
    InventoryTooLarge,
    build_inventory,
    classify_role,
    detect_language,
    path_problem,
)
from repolens.repository.mirror import (
    MirrorError,
    MirrorStore,
    RevisionAmbiguous,
    RevisionKind,
    RevisionNotFound,
)
from tests.support.gitrepo import FixtureRepo


@pytest.fixture
def runner(tmp_path: Path) -> GitRunner:
    return GitRunner(tmp_path / "home", GitPolicy(allowed_protocols=("file",)))


@pytest.fixture
def store(tmp_path: Path, runner: GitRunner) -> MirrorStore:
    return MirrorStore(tmp_path / "mirrors", runner)


@pytest.fixture
def source(tmp_path: Path) -> FixtureRepo:
    repo = FixtureRepo(tmp_path / "source", branch="trunk")
    repo.commit("first", {"pkg/__init__.py": "", "pkg/core.py": "def f():\n    return 1\n"})
    return repo


def _fetch(store: MirrorStore, source: FixtureRepo, repo_id: uuid.UUID | None = None) -> Path:
    return store.fetch(repo_id or uuid.uuid4(), source.url, max_bytes=50 * 1024 * 1024)


class TestResolution:
    def test_branch_tag_commit_and_head(self, store: MirrorStore, source: FixtureRepo) -> None:
        first = source.git("rev-parse", "HEAD")
        source.tag("v1")
        second = source.commit("second", {"b.py": "x = 1\n"})
        source.branch("feature", first)
        mirror = _fetch(store, source)

        head = store.resolve(mirror, "HEAD", default_branch=None)
        assert (head.commit_sha, head.kind, head.full_ref) == (
            second,
            RevisionKind.BRANCH,
            "refs/heads/trunk",
        )
        assert store.default_branch(mirror) == "trunk"
        assert store.resolve(mirror, "feature", default_branch=None).commit_sha == first
        tag = store.resolve(mirror, "v1", default_branch=None)
        assert (tag.commit_sha, tag.kind) == (first, RevisionKind.TAG)
        commit = store.resolve(mirror, first, default_branch=None)
        assert (commit.commit_sha, commit.kind) == (first, RevisionKind.COMMIT)
        assert store.resolve(mirror, first[:10], default_branch=None).commit_sha == first

    def test_annotated_tag_is_peeled_to_commit(
        self, store: MirrorStore, source: FixtureRepo
    ) -> None:
        sha = source.git("rev-parse", "HEAD")
        source.git("tag", "-a", "v2", "-m", "release")
        mirror = _fetch(store, source)
        assert store.resolve(mirror, "v2", default_branch=None).commit_sha == sha

    def test_branch_and_tag_with_same_name_are_ambiguous(
        self, store: MirrorStore, source: FixtureRepo
    ) -> None:
        source.tag("release")
        source.commit("later", {"c.py": ""})
        source.branch("release")
        source.git("branch", "-f", "release", "HEAD")
        source.git("tag", "-f", "release", "HEAD~1")
        mirror = _fetch(store, source)
        with pytest.raises(RevisionAmbiguous):
            store.resolve(mirror, "release", default_branch=None)

    def test_missing_revision(self, store: MirrorStore, source: FixtureRepo) -> None:
        mirror = _fetch(store, source)
        with pytest.raises(RevisionNotFound):
            store.resolve(mirror, "does-not-exist", default_branch=None)
        with pytest.raises(RevisionNotFound):
            store.resolve(mirror, "deadbeef", default_branch=None)

    def test_option_like_refs_are_rejected_before_git(
        self, store: MirrorStore, source: FixtureRepo
    ) -> None:
        mirror = _fetch(store, source)
        with pytest.raises(ValueError):
            store.resolve(mirror, "--output=/tmp/x", default_branch=None)

    def test_refetch_picks_up_new_commits(self, store: MirrorStore, source: FixtureRepo) -> None:
        repo_id = uuid.uuid4()
        mirror = _fetch(store, source, repo_id)
        new = source.commit("new", {"n.py": ""})
        assert store.resolve(mirror, "trunk", default_branch=None).commit_sha != new
        _fetch(store, source, repo_id)
        assert store.resolve(mirror, "trunk", default_branch=None).commit_sha == new

    def test_commit_info(self, store: MirrorStore, source: FixtureRepo) -> None:
        mirror = _fetch(store, source)
        sha = source.git("rev-parse", "HEAD")
        info = store.commit_info(mirror, sha)
        assert info.tree_sha == source.git("rev-parse", "HEAD^{tree}")
        assert info.committed_at.year == 2026

    def test_oversized_mirror_is_removed(self, store: MirrorStore, source: FixtureRepo) -> None:
        source.commit("big", {"blob.bin": bytes(range(256)) * 4096})
        repo_id = uuid.uuid4()
        with pytest.raises(MirrorError, match="limit"):
            store.fetch(repo_id, source.url, max_bytes=10_000)
        assert not store.path_for(repo_id).exists()

    def test_fetch_failure_is_retryable(self, store: MirrorStore, tmp_path: Path) -> None:
        with pytest.raises(MirrorError) as info:
            store.fetch(uuid.uuid4(), (tmp_path / "nope").as_uri(), max_bytes=10**6)
        assert info.value.retryable


class TestInventory:
    def test_inventory_contents(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        source.write({"tests/test_core.py": "def test_f():\n    pass\n", "README.md": "# r\n"})
        source.write({"data.bin": b"\x00\x01binary", "pyproject.toml": "[project]\n"})
        source.write({"scripts/run.sh": "#!/bin/sh\n", "ünïcode/модуль.py": "x = 1\n"})
        (source.path / "scripts/run.sh").chmod(0o755)
        source.symlink("escape", "/etc/passwd")
        sha = source.commit("files")
        mirror = _fetch(store, source)

        inventory = build_inventory(runner, mirror, sha, max_files=100, max_hash_bytes=10**6)
        by_path = {e.path: e for e in inventory.entries}
        assert set(by_path) == {
            "pkg/__init__.py",
            "pkg/core.py",
            "tests/test_core.py",
            "README.md",
            "data.bin",
            "pyproject.toml",
            "scripts/run.sh",
            "ünïcode/модуль.py",
            "escape",
        }
        core = by_path["pkg/core.py"]
        expected = hashlib.sha256(b"def f():\n    return 1\n").hexdigest()
        assert (core.language, core.role, core.content_sha256) == (
            "python",
            FileRole.SOURCE,
            expected,
        )
        assert by_path["tests/test_core.py"].role is FileRole.TEST
        assert by_path["pyproject.toml"].role is FileRole.CONFIG
        assert by_path["README.md"].role is FileRole.DOC
        assert by_path["data.bin"].is_binary and by_path["data.bin"].language is None
        assert by_path["scripts/run.sh"].mode is FileMode.EXECUTABLE
        link = by_path["escape"]
        assert link.mode is FileMode.SYMLINK and link.language is None
        assert link.role is FileRole.OTHER  # recorded, never followed
        assert by_path["ünïcode/модуль.py"].language == "python"
        assert inventory.language_counts()["python"] == 4

    def test_fingerprint_is_deterministic_and_content_sensitive(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        first = source.git("rev-parse", "HEAD")
        second = source.commit("edit", {"pkg/core.py": "def f():\n    return 2\n"})
        mirror = _fetch(store, source)

        def fingerprint(sha: str) -> str:
            return build_inventory(
                runner, mirror, sha, max_files=10, max_hash_bytes=10**6
            ).fingerprint()

        assert fingerprint(first) == fingerprint(first)
        assert fingerprint(first) != fingerprint(second)

    def test_file_count_limit(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        sha = source.commit("many", {f"m{i}.py": "" for i in range(20)})
        mirror = _fetch(store, source)
        with pytest.raises(InventoryTooLarge):
            build_inventory(runner, mirror, sha, max_files=5, max_hash_bytes=10**6)

    def test_large_files_are_not_hashed(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        sha = source.commit("big", {"big.py": "x = 1\n" * 1000})
        mirror = _fetch(store, source)
        inventory = build_inventory(runner, mirror, sha, max_files=10, max_hash_bytes=100)
        big = next(e for e in inventory.entries if e.path == "big.py")
        assert big.content_sha256 is None and big.size_bytes == 6000
        assert any(i.code == "FILE_NOT_HASHED" for i in inventory.issues)

    def test_submodules_are_recorded_not_fetched(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        source.git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/lib")
        source.git("commit", "-q", "-m", "submodule")
        sha = source.git("rev-parse", "HEAD")
        mirror = _fetch(store, source)
        inventory = build_inventory(runner, mirror, sha, max_files=10, max_hash_bytes=10**6)
        sub = next(e for e in inventory.entries if e.path == "vendor/lib")
        assert sub.mode is FileMode.SUBMODULE and sub.content_sha256 is None
        assert any(i.code == "SUBMODULE_NOT_FETCHED" for i in inventory.issues)

    def test_case_collisions_are_reported(
        self, store: MirrorStore, runner: GitRunner, source: FixtureRepo
    ) -> None:
        sha = source.commit("case", {"Readme.py": "", "README.py": ""})
        mirror = _fetch(store, source)
        inventory = build_inventory(runner, mirror, sha, max_files=10, max_hash_bytes=10**6)
        assert [i.code for i in inventory.issues] == ["PATH_CASE_COLLISION"]


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("a/b.py", True),
        ("ünïcode.py", True),
        ("/etc/passwd", False),
        ("a/../b", False),
        ("./a", False),
        ("a//b", False),
        (".git/config", False),
        ("sub/.GIT/hooks/x", False),
        ("a\\b", False),
        ("a\x01b", False),
        ("", False),
    ],
)
def test_path_problem(path: str, ok: bool) -> None:
    assert (path_problem(path) is None) is ok


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("src/app.py", FileRole.SOURCE),
        ("tests/test_app.py", FileRole.TEST),
        ("app/test_views.py", FileRole.TEST),
        ("app/views_test.py", FileRole.TEST),
        ("conftest.py", FileRole.TEST),
        ("tests/helpers.py", FileRole.TEST),
        ("setup.py", FileRole.CONFIG),
        ("config/settings.yaml", FileRole.CONFIG),
        ("docs/index.md", FileRole.DOC),
        ("LICENSE", FileRole.OTHER),
        ("web/app.ts", FileRole.SOURCE),
    ],
)
def test_classify_role(path: str, role: FileRole) -> None:
    assert classify_role(path) is role


def test_detect_language_is_case_insensitive() -> None:
    assert detect_language("A.PY") == "python"
    assert detect_language("Makefile") is None
