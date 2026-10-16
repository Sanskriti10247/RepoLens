"""Create the inventory fixture repository (tags: base, change) for demos and e2e tests.

Usage: python scripts/make_fixture_repo.py <directory> [--regression]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support import projects
from tests.support.gitrepo import FixtureRepo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--regression", action="store_true", help="make the change break tests")
    args = parser.parse_args()
    if args.directory.exists():
        sys.exit(f"{args.directory} already exists")
    repo = FixtureRepo(args.directory)
    repo.commit("add inventory project", projects.INVENTORY)
    repo.tag("base")
    change = projects.REGRESSION_WITH_TAX if args.regression else projects.CHANGE_WITH_TAX_BODY
    repo.commit("change tax calculation", change)
    repo.tag("change")
    print(repo.url)


if __name__ == "__main__":
    main()
