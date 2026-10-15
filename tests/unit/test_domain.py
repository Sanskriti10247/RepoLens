import pytest

from repolens.domain.enums import JobStatus
from repolens.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    allowed_transitions,
    can_transition,
    sources_for,
)
from repolens.domain.spans import Span


class TestSpan:
    def test_valid_span(self) -> None:
        span = Span(3, 4, 5, 0)
        assert span.line_count == 3
        assert span.contains_line(4)
        assert not span.contains_line(6)

    def test_single_line_span(self) -> None:
        assert Span(1, 0, 1, 10).line_count == 1

    @pytest.mark.parametrize("args", [(0, 0, 1, 0), (1, -1, 1, 0), (2, 0, 1, 5), (3, 5, 3, 4)])
    def test_invalid_spans_are_rejected(self, args: tuple[int, int, int, int]) -> None:
        with pytest.raises(ValueError):
            Span(*args)

    def test_overlap(self) -> None:
        span = Span(10, 0, 20, 0)
        assert span.overlaps_lines(20, 25)
        assert span.overlaps_lines(1, 10)
        assert not span.overlaps_lines(21, 30)
        assert not span.overlaps_lines(1, 9)


class TestJobStateMachine:
    def test_happy_path(self) -> None:
        assert can_transition(JobStatus.QUEUED, JobStatus.CLAIMED)
        assert can_transition(JobStatus.CLAIMED, JobStatus.RUNNING)
        assert can_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_JOB_STATUSES))
    def test_terminal_states_are_final(self, terminal: JobStatus) -> None:
        assert allowed_transitions(terminal) == frozenset()

    def test_cannot_skip_claim(self) -> None:
        assert not can_transition(JobStatus.QUEUED, JobStatus.RUNNING)
        assert not can_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)

    def test_sources_for(self) -> None:
        assert sources_for(JobStatus.SUCCEEDED) == {JobStatus.RUNNING}
        assert sources_for(JobStatus.CANCELLED) == {
            JobStatus.QUEUED,
            JobStatus.CLAIMED,
            JobStatus.RUNNING,
        }
