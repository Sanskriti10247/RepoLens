import json

import pytest

from repolens.config import LogFormat
from repolens.observability.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)


def test_json_logs_carry_context_and_redact_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)
    clear_context()
    bind_context(request_id="req-123")
    get_logger("test").info("handled", token="abc", path="/api/health")
    clear_context()

    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "handled"
    assert record["request_id"] == "req-123"
    assert record["token"] == "[redacted]"
    assert record["level"] == "info"


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", LogFormat.JSON)
    get_logger("test").info("hidden")
    assert capsys.readouterr().err == ""
