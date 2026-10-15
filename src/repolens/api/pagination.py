from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Pagination = Annotated[PageParams, Depends(page_params)]
