# Demo walkthrough

This walkthrough runs the whole workflow offline. It uses a small fixture repository with
six tests and two tags:
- `base`;
- `change`, which edits `inventory.pricing.with_tax`.

The browser test in `apps/web/e2e/workflow.spec.ts` automates these same steps and checks
each result. Run it with `./scripts/e2e.sh`. To save screenshots of each step, also set
`E2E_SCREENSHOTS=<dir>`.

## Setup

Follow **Running locally** in the README up to building the sandbox image. Then create the
fixture and allow local repositories, for this demo only:

```bash
mkdir -p /tmp/repolens-demo
uv run python scripts/make_fixture_repo.py /tmp/repolens-demo/inventory   # prints its file:// URL
# add --regression to make the change break three tests instead

export REPOLENS_ALLOW_LOCAL_REPOSITORIES=true
export REPOLENS_LOCAL_REPOSITORY_ROOTS='["/tmp/repolens-demo"]'
uv run repolens-api & uv run repolens-worker &
REPOLENS_API_TOKEN=... npm run web:dev    # http://127.0.0.1:3100
```

## Steps

1. **Ingest.** On the home page, enter the fixture URL and the revision `change`, then choose
   **Register and analyze**. The overview shows the analysis as succeeded, with
   *6 tests discovered statically*.
2. **Explore.** Open **Code explorer**, search `with_tax` and pick
   `inventory.pricing.with_tax`. You get:
   - its source;
   - two callers, including `inventory.pricing.quote`;
   - any references that could not be resolved, listed as such.
3. **Impact.** Open **Impact**, set base `base` and target `change`, then choose
   **Analyze impact**. The report shows:
   - the changed symbol and the impacted symbols, each with the path back to the change
     (`inventory.reports.summary` reaches it through `quote`);
   - the risk factors;
   - completeness: *all changes were traced to symbols and tests*.
4. **Select tests.** Choose **Select tests**. The plan selects 3 of 6 tests. Each selected test
   lists its evidence (direct or transitive static dependency), and each excluded test, such
   as `tests/test_util.py::test_slugify`, says why it was left out.
5. **Execute.** Choose **Run 3 selected tests**. They run in the sandbox: no network, non-root,
   read-only source. The run page shows the outcome, the result of each test, the logs and
   coverage evidence. With `--regression`, the same three tests fail, and the full suite finds
   no failure that the selection missed (see [benchmarks](benchmarks/)).
6. **Ask.** Open **Ask** and enter *Who calls with_tax?* The answer comes from static
   relationships in the graph, with citations. No model provider is needed.

The **Queue** page shows every job behind these steps, with attempts, timings and any errors.
