from __future__ import annotations

import uvicorn

from repolens.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "repolens.api.app:build",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
