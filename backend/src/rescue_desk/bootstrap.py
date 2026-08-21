from __future__ import annotations

import uvicorn
from alembic import command
from alembic.config import Config

from rescue_desk.config import get_settings
from rescue_desk.seed_demo import main as seed_demo_main


def main() -> None:
    settings = get_settings()
    command.upgrade(Config("alembic.ini"), "head")
    if settings.seed_demo:
        seed_demo_main()
    uvicorn.run(
        "rescue_desk.main:app",
        host="0.0.0.0",
        port=8000,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
