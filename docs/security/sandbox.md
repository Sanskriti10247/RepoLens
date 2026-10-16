# Sandboxed test execution: threat model and limits

RepoLens runs tests from repositories it does not trust. This document describes what the
sandbox protects, how, what was verified, and what it does **not** protect against.

## Assets and attacker

- **Protected:** the host (files, credentials, processes, network position), the RepoLens
  database and worker, other repositories' data, and the availability of the machine.
- **Attacker:** anyone who controls the contents of an analysed repository — source files,
  tests, `conftest.py`, packaging metadata, symlinks and Git objects.
- **Out of scope:** a malicious operator, a compromised Docker daemon or kernel, and a
  compromised sandbox image build (the image is built by the operator from pinned inputs).

## Where repository code can run

| Stage | Runs repository code? | Where |
|---|---|---|
| Fetching and inventory | No — bare mirror, objects read with `git cat-file` | worker |
| Parsing and analysis | No — Tree-sitter reads bytes | worker |
| Test discovery (static) | No | worker |
| Test collection (`pytest --collect-only`) | **Yes** (imports modules) | sandbox |
| Test execution | **Yes** | sandbox |
| Dependency installation | Not performed (decision D-016) | — |

## Controls

Each container is started by the worker with the `docker` CLI using an argument vector
(`src/repolens/execution/docker.py`):

| Control | Flag / mechanism |
|---|---|
| Unprivileged user | `--user 10001:10001` (image user `sandbox`, no login shell) |
| No capabilities | `--cap-drop ALL`, `--security-opt no-new-privileges` |
| Syscall filtering | Docker's default seccomp profile (not disabled) |
| Immutable root filesystem | `--read-only` |
| Bounded writable space | `--tmpfs /work` (repository copy, size-limited), `--tmpfs /tmp` (`noexec`) |
| No network | `--network none` |
| Resource limits | `--pids-limit`, `--memory` = `--memory-swap` (no extra swap), `--cpus`, `--ulimit nofile`, `--ulimit core=0:0` (no core dumps, whatever the host's `core_pattern`) |
| Wall-clock limit | enforced by the worker; the container is killed with `docker kill` |
| Signal handling / zombies | `--init` |
| No host access | only the materialised snapshot is mounted, read-only, at `/src`; no Docker socket; no host environment is passed (`docker run` receives no `-e`) |
| Bounded output | stdout/stderr streamed and capped; Docker logging disabled (`--log-driver none`) |
| No result files on the host | results leave only through a size-capped stdout envelope; a test cannot fill the host disk through a writable mount |
| Cleanup | container inspected then force-removed after every run; workspace deleted; a janitor removes leftovers at worker start |

Inputs are validated before a container starts (`src/repolens/execution/policy.py`): the image
must be the configured, approved image; limits must be within hard ceilings; test identifiers
must be repository-relative pytest node ids (no leading `-` or `/`, no `..`, no control
characters). The API exposes no way to pass commands or arbitrary arguments.

The snapshot is written into the workspace from Git objects (`repository/materialize.py`) with
`O_NOFOLLOW | O_EXCL` relative to directory descriptors; symlinks are created only after all
regular files and only when their target stays inside the workspace. After all links exist,
each one is resolved on disk, and any link that leaves the workspace through a chain of
otherwise-contained links is removed (`SYMLINK_ESCAPES`).

## Verified behaviour

`tests/security/test_sandbox.py` runs real attacks inside the real container (Docker 29.7,
runc 1.5, cgroup v2, on the development machine):

| Scenario | Result |
|---|---|
| Runs as non-root, `CapEff` is zero, no Docker socket, host files invisible, no host environment, root filesystem and `/src` read-only, network unreachable | all asserted from inside the container |
| Infinite loop | `TIMED_OUT`, container removed |
| Cancellation mid-run | `CANCELLED` within seconds, container removed |
| Fork bomb | contained by the PID limit |
| Allocating 1 GiB with a 128 MiB limit | `RESOURCE_LIMIT_EXCEEDED` (never `PASSED`) |
| Filling the workspace | contained in tmpfs; host disk unaffected; `RESOURCE_LIMIT_EXCEEDED` |
| Detached background process | does not survive the container |
| Flooding the runner's stdout via `/proc/1/fd/1` | output capped; reported as `INFRASTRUCTURE_ERROR` |
| Failing / uncollectable tests | `FAILED` / `COLLECTION_ERROR` |

`tests/security/test_git_runner.py` and `tests/security/test_materialize.py` cover hooks,
fsmonitor, dangerous transports, crafted trees, symlink escapes and path traversal.

## Residual risks (not mitigated)

- **Kernel and runtime escapes.** Containers share the host kernel; Docker here is rootful
  with `runc`. A kernel or runtime vulnerability could lead to host compromise. Stronger
  isolation (gVisor `runsc`, Kata, or per-run VMs) fits behind the same backend interface but
  is not implemented.
- **AppArmor/SELinux** are not active on the development host, so no mandatory access control
  profile is applied beyond seccomp.
- **Result integrity.** The repository's tests run in the same container as the runner and
  as the same user. A malicious repository can make its own tests report anything (it can do
  that with `assert True` anyway). The sandbox protects the host; it does not make a
  repository's self-reported results trustworthy. One check applies: `PASSED` requires that
  every requested test reported a result, and that at least one result exists. A repository
  configuration that deselects tests (`addopts`, `conftest.py` hooks) and still exits 0 is
  reported as `COLLECTION_ERROR`. Repository pytest configuration is otherwise honoured,
  because real projects depend on it.
- **CPU and I/O contention.** CPU is capped per container and concurrent sandboxes are capped
  across workers (`REPOLENS_MAX_CONCURRENT_SANDBOXES`), but block I/O is not limited.
- **Disk use while fetching.** The repository size limit is checked against GitHub's reported
  size before the fetch and against the mirror on disk after it. During the fetch, only the
  Git timeout bounds how much is written. A repository whose reported size is wrong can use
  disk space in the data directory until the timeout or the post-fetch check removes it.
- **Dependencies.** Without dependency installation, repositories that need third-party
  packages fail collection (`COLLECTION_ERROR`). Enabling installation would require a
  separate, network-enabled stage and a new approval (D-016).
- **Image supply chain.** The base image is pinned by digest and Python packages are
  hash-pinned, but the image is only as trustworthy as those upstream artifacts.
