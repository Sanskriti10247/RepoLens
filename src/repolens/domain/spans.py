"""Source locations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class Span:
    """A source range. Lines are 1-based and inclusive; columns are 0-based byte offsets.

    The end column is exclusive (it points just past the last byte), matching Tree-sitter.
    """

    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def __post_init__(self) -> None:
        if self.start_line < 1 or self.start_col < 0 or self.end_col < 0:
            raise ValueError(f"invalid span start/columns: {self}")
        if (self.end_line, self.end_col) < (self.start_line, self.start_col):
            raise ValueError(f"span ends before it starts: {self}")

    def contains_line(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line

    def overlaps_lines(self, first: int, last: int) -> bool:
        return self.start_line <= last and first <= self.end_line

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1
