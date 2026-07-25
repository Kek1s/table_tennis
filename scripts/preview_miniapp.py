from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from table_tennis_bot.config import Settings
from table_tennis_bot.database import Database
from table_tennis_bot.webapp import create_web_application


def main() -> None:
    settings = replace(
        Settings.from_env(),
        database_path=Path(tempfile.gettempdir()) / "table-tennis-miniapp-preview.sqlite3",
        web_host="127.0.0.1",
        web_port=8090,
        webapp_dev_mode=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    web.run_app(
        create_web_application(database, settings),
        host=settings.web_host,
        port=settings.web_port,
    )


if __name__ == "__main__":
    main()
