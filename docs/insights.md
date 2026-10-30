# Repository health report

`GET /api/analyses/{id}/insights` summarises one finished analysis: a score out of 100 with
a letter grade, the metrics behind it, and a list of findings. Everything is computed from
the stored results of the analysis (symbols, references, dependency edges and the file
inventory), so the numbers are reproducible and nothing is sampled or estimated. For a
`REUSED` analysis the report is read from the run that owns the results
(`results_run_id`).

The code lives in `services/insights.py` (scoring and finding rules; the constants there are
the values quoted below) and `persistence/insight_queries.py` (the aggregate queries).

## What counts as what

- **Test code** is any file whose inventory role is `TEST`: `test_*.py`, `*_test.py`,
  `conftest.py`, or a `.py` file under a `tests/`, `test/` or `testing/` directory.
  Complexity, size, documentation, coupling and dead-code figures cover non-test code only.
- **Functions** are functions and methods, sync or async, including nested ones.
- **Public symbols** are classes, functions and methods in non-test files where no part of
  the qualified name starts with `_` and the symbol is not local to a function. Methods of a
  private class, everything in a private module (`pkg._compat`) and everything in a script
  that is not importable (RepoLens names those `__file__.<path>`) are therefore not public.
- **Modules** are analysed source files (Python, JavaScript, TypeScript, Go, Java, Rust). A module is **testable** if it defines at least one
  class or function, so an `__init__.py` that only re-exports names is not counted as untested.
- **Lines** of a function are `end_line - start_line + 1` of its definition, decorators
  included. Lines of a module are its line count. File sizes and the line count per language
in `overview.languages` come from the inventory, which counts every text file (`null` lines
for snapshots taken before line counting).

## Cyclomatic complexity

Measured by the Python parser for every function and method and stored in
`symbols.complexity`. Classes and modules have none.

complexity = 1 + one for each of:

| Construct | Counts |
|---|---|
| `if`, `elif` | 1 each (a plain `else` adds nothing) |
| `for`, `while` (including `async for`) | 1 each |
| `else` on a `for`, `while` or `try` | 1 |
| `except` / `except*` clause | 1 each |
| `and`, `or` | 1 per operator (`a and b or c` is 2) |
| conditional expression `x if c else y` | 1 |
| comprehension `for` and `if` | 1 each |
| `case` clause of a `match` | 1 each, except an irrefutable `case _:` |
| guard `case … if cond:` | 1 |

Not counted: `with`, `assert`, `return`/`break`/`continue`, `finally`, `try` itself.
This follows McCabe's definition as implemented by common Python tools (radon, mccabe),
except that `assert` is not a branch here. Only the function's own body is measured.
Nested functions and classes are separate symbols with their own value. Branches inside a
`lambda` count toward the enclosing function, because lambdas are not symbols.

The other languages use the same rule over their own syntax: `if`, loops (`for`, `for…in`,
`while`, `do`), `catch`, the ternary `?:`, each `case` of a `switch` or `match` arm except
`default` and `_`, and each `&&`, `||` and `??`. Nested named functions, methods and classes
are measured separately; anonymous functions are not measured at all.

Analyses parsed before complexity was recorded have `NULL` there. Their report has
`complexity.available = false`, and the complexity component is left out of the overall
score (see below). Adding complexity changed the parser version, so a new analysis of the
same commit parses it again rather than reusing the old results or the parse cache.

## Score

Five components, each scored 0-100:

| Component | Weight | Formula |
|---|---|---|
| `complexity` | 0.25 | `100 × (1 − 4 × (n₁₁₋₂₀ + 2 × n₂₁₊) / n)`, where `n` is the number of measured functions and `n₁₁₋₂₀`, `n₂₁₊` those with complexity 11-20 and 21 or more |
| `documentation` | 0.15 | `100 × documented / public`: public symbols with a docstring |
| `testing` | 0.25 | `100 × tested / testable`: testable modules that some test file imports or references |
| `structure` | 0.20 | `100 × (1 − 2 × (in_cycles + high_fan_out) / modules)`: non-test modules in an import cycle, and those depending on more than 15 other modules |
| `resolution` | 0.15 | `100 × resolved / (resolved + ambiguous + unresolved + dynamic)` |

Each formula is clamped to 0-100 and rounded. The examples behind the factors: 5% of
functions above 10 costs the complexity component 20 points (2.5% if they are all 21+);
one module in ten caught in a cycle costs structure 20 points.

The overall score is the weighted mean, rounded. If a component has no data (an analysis
without complexity, a repository with no public symbols or no testable modules), its score
is `null`, its weight is 0, its `detail` says it was excluded, and the other weights are
divided by their sum so they still add up to 1. The `weight` field always holds the weight
actually used, so `overall = round(Σ weight × score)`.

Grades: **A** 90 and above, **B** 75-89, **C** 60-74, **D** 45-59, **F** below 45.

### Definitions used by the components

- **Tested module.** Any dependency edge (call, import, reference or inheritance; not
  containment) from a symbol in a test file to a symbol in the module. Importing the module,
  or a name from it, is enough. This is reachability, not line coverage: a module imported
  by a test but never exercised still counts as tested. Coverage from sandboxed test runs
  is not used here.
- **Import cycle.** A strongly connected component of the graph of module-level imports
  that resolved to another non-test module. Imports inside functions (deferred imports, the
  usual way to break a cycle) and imports inside `if TYPE_CHECKING:` blocks are left out,
  because neither runs when the module is imported. Analyses parsed before the
  `TYPE_CHECKING` flag existed count those imports too.
- **Fan-in / fan-out.** The number of distinct other non-test modules that depend on a module,
  or that it depends on, through any non-containment edge (including type-only imports).
  `instability = fan_out / (fan_in + fan_out)`.
- **Resolution rate.** Of the references that could point into the repository, the share
  that resolved to exactly one definition. References to the standard library and
  third-party packages (`EXTERNAL`) are left out, since they are resolved, just not to
  repository code. The `references` object reports the count for every stored status.

## Findings

Rule based. Each finding has a severity (`high`, `medium`, `low`, `info`), a plain
description and up to five pieces of evidence (path and line, and the symbol and file ids for
links). Findings are sorted by severity, then by the amount of evidence.

| Id | Fires when | Severity |
|---|---|---|
| `complexity.very_high` | any function has complexity 21 or more | high |
| `complexity.elevated` | any function has complexity 11-20 | medium if 5% or more of functions are above 10, else low |
| `size.long_functions` | any function is longer than 100 lines | medium if 5 or more, else low |
| `size.long_modules` | any non-test module is longer than 1000 lines | low |
| `documentation.low_coverage` | fewer than 80% of public symbols have a docstring | medium below 50%, else low |
| `testing.no_tests` | there are testable modules but no test files | high |
| `testing.untested_modules` | some testable modules are not reached from tests | high below 50% tested, medium below 80%, else low |
| `structure.import_cycles` | any import cycle | medium |
| `structure.high_fan_out` | a module depends on more than 15 others | low |
| `structure.hubs` | a definition has 25 or more distinct callers | info |
| `dead_code.candidates` | any dead-code candidate (below) | low |
| `resolution.low` | resolution rate below 80% | medium below 60%, else low |
| `analysis.incomplete_files` | source files parsed with errors, skipped as too large or undecodable, or failed | medium if 10% or more of source files, else info |

## Dead-code candidates

Static analysis cannot see plugins, dynamic dispatch, reflection or callers outside the
repository, so the rules flag only what is unlikely to be used from elsewhere and leave out
anything with a plausible hidden caller. A definition is a candidate only when all of these
hold:

- it is a class, function or method in a source file (not tests, docs, configuration or a
  `.pyi` stub), with no parse errors in its span;
- it is not public (see above). Public names of importable modules may be used by other
  projects and are never flagged. A method is flagged only if its own name is private
  (`_name`): public method names often implement a protocol (`write`, `get_command`) that
  callers outside the repository use;
- it is not a dunder, not decorated (routes, fixtures, properties, registrations) and not
  named `main`;
- no dependency edge other than containment points at it, or at another definition with the
  same qualified name (functions defined differently in two branches of an `if`);
- its name does not occur in the text of any unresolved, ambiguous or dynamic reference
  (`obj._helper()` on an object of unknown type, `getattr(self, "_helper")`), nor in any
  function signature (a class used only in annotations);
- it is not a method of a class whose bases, directly or through other repository classes,
  include one that is not a repository class: that base may call it.

`__all__` is not stored with the analysis, so it is not consulted. It rarely matters here:
names listed in `__all__` are public, and public names are not flagged anyway.

Since this revision the parser also records functions and classes listed in dict, list,
tuple and set literals (dispatch tables such as `{"stdin": get_stdin}`) and `metaclass=`
arguments as references, so those are no longer reported as unused.

## Cost

The report runs about twenty aggregate queries, each scoped to the run's indexed rows.
Every list in the response is capped (10 items, 5 per finding). Only three queries return
more than a handful of rows: one row per module, one per distinct pair of files linked by a
dependency, and one per pair linked by an import-time import. SQL reduces the edges to
those pairs, so the symbol graph is never loaded into Python. On [pallets/click](https://github.com/pallets/click)
(2,169 symbols, 8,912 references, 5,982 edges) the endpoint answers in 80-100 ms on a
development laptop, with the API process at about 120 MB resident.
