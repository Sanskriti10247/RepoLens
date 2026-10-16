from __future__ import annotations

import signal
import threading
from datetime import timedelta
from types import FrameType

from repolens.config import get_settings
from repolens.observability.logging import configure_logging, get_logger
from repolens.persistence.db import make_engine, make_session_factory
from repolens.services.execution import cleanup_stale_execution
from repolens.worker.handlers import build_handlers
from repolens.worker.runtime import Worker


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    cleanup_stale_execution(settings)
    engine = make_engine(settings.database_url, pool_size=2)
    session_factory = make_session_factory(engine)
    worker = Worker(
        session_factory,
        build_handlers(settings),
        lease=timedelta(seconds=settings.job_lease_seconds),
    )
    stop = threading.Event()

    def _stop(signum: int, _: FrameType | None) -> None:
        get_logger(__name__).info("worker_stop_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        worker.run_forever(stop)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
