"""RepoLens sandbox runner (runs inside the test container; standard library only).

Trusted code baked into the sandbox image. It copies the read-only snapshot into a
size-limited tmpfs, runs pytest (optionally under coverage) with its output redirected to
files, and prints a single result envelope line to stdout:

    REPOLENS-RESULT {"version": 1, ...}

Note on integrity: the repository's tests run in the same container as this script, so a
malicious repository can influence what its own tests report. The sandbox protects the host;
it does not make a repository's self-reported results trustworthy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENVELOPE_PREFIX = "REPOLENS-RESULT "
VERSION = 1
TAIL_BYTES = 64 * 1024
MAX_JUNIT_BYTES = 4 * 1024 * 1024
MAX_COVERAGE_BYTES = 8 * 1024 * 1024
COVERAGE_RC = """\
[run]
branch = False
data_file = {out}/.coverage
dynamic_context = test_function
source = {repo}
omit = */tests/*, */test_*.py, */*_test.py, */conftest.py
[json]
show_contexts = True
"""


def tail(path: Path, limit: int = TAIL_BYTES) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        data = handle.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def bounded(path: Path, limit: int) -> tuple[str | None, bool]:
    if not path.exists():
        return None, False
    if path.stat().st_size > limit:
        return None, True
    return path.read_text(encoding="utf-8", errors="replace"), False


def emit(result: dict[str, object]) -> None:
    sys.stdout.write("\n" + ENVELOPE_PREFIX + json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def prepare(source: Path, work: Path) -> Path:
    repo = work / "repo"
    shutil.copytree(source, repo, symlinks=True)
    return repo


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("mode", choices=["run", "collect"])
    parser.add_argument("--source", default="/src")
    parser.add_argument("--work", default="/work")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("node_ids", nargs="*")
    args = parser.parse_args(argv)
    work = Path(args.work)
    out = work / ".repolens"
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result: dict[str, object] = {"version": VERSION, "mode": args.mode}
    try:
        repo = prepare(Path(args.source), work)
    except OSError as exc:
        result.update(status="workspace_error", error=f"{type(exc).__name__}: {exc}")
        emit(result)
        return 0

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONUNBUFFERED": "1",
        "CI": "1",
    }
    base = ["-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(repo)]
    if args.mode == "collect":
        command = [sys.executable, *base, "--collect-only", "-q"]
    else:
        command = [
            sys.executable,
            *base,
            "-q",
            "-o",
            "junit_family=xunit2",
            "--junitxml",
            str(out / "junit.xml"),
        ]
        if args.coverage:
            rc = out / "coveragerc"
            rc.write_text(COVERAGE_RC.format(out=out, repo=repo))
            command = [sys.executable, "-m", "coverage", "run", f"--rcfile={rc}", *command[1:]]
    command += ["--", *args.node_ids] if args.node_ids else []

    stdout_path, stderr_path = out / "stdout.txt", out / "stderr.txt"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(
            command, cwd=repo, env=env, stdout=stdout, stderr=stderr, check=False
        )
    result["pytest_exit_code"] = completed.returncode
    stats = os.statvfs(work)
    result["workspace_free_bytes"] = stats.f_bavail * stats.f_frsize
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["stdout_tail"], result["stdout_truncated"] = tail(stdout_path)
    result["stderr_tail"], result["stderr_truncated"] = tail(stderr_path)

    if args.mode == "collect":
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
        result["collected"] = [line.strip() for line in text.splitlines() if "::" in line]
    else:
        result["junit"], result["junit_truncated"] = bounded(out / "junit.xml", MAX_JUNIT_BYTES)
        if args.coverage and (out / ".coverage").exists():
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    f"--rcfile={out / 'coveragerc'}",
                    "-o",
                    str(out / "coverage.json"),
                ],
                cwd=repo,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            result["coverage"], result["coverage_truncated"] = bounded(
                out / "coverage.json", MAX_COVERAGE_BYTES
            )
    result["status"] = "completed"
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
