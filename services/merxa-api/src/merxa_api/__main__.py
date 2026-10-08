"""Entrypoint: ``python -m merxa_api`` (uvicorn on $PORT, default 8100)."""

from __future__ import annotations

import uvicorn

from merxa_api.app import create_app
from merxa_api.config import Settings
from merxa_api.logs import configure_logging


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
        access_log=False,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
