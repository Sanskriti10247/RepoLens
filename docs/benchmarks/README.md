# Benchmarks

Raw results: [results-2026-10-16.json](results-2026-10-16.json). Reproduce with:

```bash
docker compose up -d postgres && ./scripts/build-sandbox.sh
uv run python scripts/benchmark.py --out docs/benchmarks/results.json \
  --repo https://github.com/pallets/itsdangerous@main --repo https://github.com/pallets/click@main \
  --repo https://github.com/pallets/flask@main --repo https://github.com/psf/requests@main
```

## Environment

| | |
|---|---|
| CPU | Intel Core i5-13420H, 12 threads |
| Memory | 15.3 GB |
| OS | Linux 7.2.5 |
| Python | 3.14.8 |
| Git | 2.55.0 |
| Docker | 29.7.2 (rootful, runc) |
| PostgreSQL | 16 in Docker Compose on the same machine |

This was a shared development laptop with other workloads running. Each figure comes from a
**single run**: expect noise of tens of percent, and treat small differences as noise.
Network fetches went over a home connection to GitHub.

## Methodology

The script drives the real worker code in-process (job queue, handlers, PostgreSQL writes)
against a dedicated `repolens_bench` database, recreated for each run. For each repository:

1. **Cold analysis** of `main`: fetch the mirror, take the snapshot, then parse, resolve and
   persist. The parse cache starts empty.
2. **Incremental analysis** of the commit five first-parent commits back from `main`, with the
   parse cache already filled by step 1.
3. **Impact** between that older commit and `main`, then a conservative test plan.
   `selection_ms` is the time to build the plan.

Then:

4. **Sandbox startup**: five `docker run`s of the sandbox image, each with one trivial test.
   The time covers create, run, report and remove.
5. **Selection quality** on the controlled fixture (`tests/support/projects.py`, 6 tests). For
   each scripted change, the selected tests and the full suite both run in the sandbox.

## Results

### Analysis

| Repository | Python files | Symbols | References | Fetch | Cold analysis | Incremental analysis | Cache hits / misses |
|---|---:|---:|---:|---:|---:|---:|---:|
| pallets/itsdangerous | 15 | 159 | 577 | 7.2 s | 153 ms | 98 ms | 11 / 4 |
| pallets/click | 91 | 2,169 | 8,884 | 9.3 s | 1,888 ms | 1,404 ms | 64 / 15 |
| pallets/flask | 83 | 1,705 | 5,233 | 9.3 s | 1,113 ms | 864 ms | 73 / 10 |
| psf/requests | 37 | 844 | 3,500 | 9.9 s | 730 ms | 540 ms | 37 / 0 |

"Analysis" covers parsing, resolution and persistence, and excludes fetch and snapshot. The
breakdown for click, the largest repository, is below:

| | Cold | Incremental |
|---|---:|---:|
| parse | 714 ms | 357 ms |
| resolve | 97 ms | 81 ms |
| persist | 990 ms | 882 ms |

What these numbers show:
- **Fetching dominates.** It takes 5–14 s per job, against at most about 2 s of analysis.
- **The parse cache halves parse time** on unchanged files.
- **Persistence is now the largest analysis cost.** Every run still writes its own complete
  graph, because results are scoped to an analysis run and never patched in place.
- **Bulk loading made a large difference.** Moving persistence to PostgreSQL `COPY` with
  pre-reserved ids cut click's persist step from 5,915 ms to about 1 s. The 5,915 ms figure
  came from an earlier run of the same script, before that change.

Reference resolution, which shows how much of the code graph is statically known:

| Repository | Resolved | External | Unresolved | Dynamic |
|---|---:|---:|---:|---:|
| itsdangerous | 37% | 33% | 28% | 2% |
| click | 45% | 29% | 22% | 4% |
| flask | 25% | 29% | 42% | 4% |
| requests | 33% | 37% | 26% | 4% |

Unresolved references are mostly calls on attributes and return values, which need type
inference. They are reported as unresolved rather than guessed, and they make impact reports
less complete.

### Impact and selection on real history

| Repository | Changed files | Changed symbols | Scope | Selected / total | Plan time |
|---|---:|---:|---|---:|---:|
| itsdangerous | 16 | 6 | FULL_SUITE | 37 / 37 | 27 ms |
| click | 42 | 248 | FULL_SUITE | 606 / 606 | 146 ms |
| flask | 15 | 26 | FULL_SUITE | 392 / 392 | 86 ms |
| requests | 3 | 0 | NONE | 0 / 347 | 66 ms |

- **Three of the four windows changed `pyproject.toml` and `uv.lock`.** For those, the
  conservative policy chose the full suite, and each report records the reason ("test or
  packaging configuration changed: pyproject.toml, uv.lock").
- **The requests window changed only CI workflow and pre-commit files.** It is reported as
  complete with no runtime impact.
- **This benchmark found a bug, since fixed.** An earlier version reported that requests
  change as `BROAD` and selected nothing. Two fixes followed:
  - CI and editor configuration are now classified as having no runtime effect.
  - A `BROAD` scope with no test linked to any change now falls back to the full suite.
- **These windows say little about selection precision.** Targeted selection is measured on
  the fixture below.

### Sandbox startup

Median **707 ms** over 5 runs (min 636 ms, max 1,073 ms). This is the fixed cost of every test
run: container create, interpreter start and pytest collection of one test.

### Selection quality on the controlled fixture

| Change | Scope | Selected | Selected run | Full run | Failures missed |
|---|---|---:|---|---|---:|
| `with_tax` body change | TARGETED | 3 / 6 | passed (780 ms) | passed (796 ms) | 0 |
| `with_tax` regression | TARGETED | 3 / 6 | **3 failed** (771 ms) | **3 failed**, 3 passed (799 ms) | 0 |
| `Item.total` change | BROAD | 5 / 6 | passed (765 ms) | passed (784 ms) | 0 |
| util-only change | TARGETED | 1 / 6 | passed (759 ms) | passed (755 ms) | 0 |
| config-only change | FULL_SUITE | 6 / 6 | passed (781 ms) | passed (827 ms) | 0 |

- **The selected tests caught the injected regression in full.** They found the same 3
  failures as the full suite.
- **The fixture is too small to show a runtime saving.** Its tests run in milliseconds, so
  container overhead (about 0.7 s) dominates both runs. The saving from selection grows with
  suite runtime, and these numbers cannot demonstrate it.

## Limitations

- **One machine, one run per figure, a shared laptop.** These are indicative numbers, not a
  controlled study.
- **Impact jobs fetch from GitHub again even when both commits are already in the mirror.**
  That fetch makes up most of the impact wall time above (3–14 s). Skipping it when both SHAs
  are present is an obvious improvement that has not been made yet.
- **The only selection-safety measurement is the fixture.** On real repositories, safety rests
  on the conservative fallbacks. No study has yet replayed historical failing commits.
- **Coverage-based evidence was not measured.** It needs an earlier sandboxed run with
  coverage on the same repository.
