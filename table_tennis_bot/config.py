from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding real environment values."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_path: Path
    admin_ids: frozenset[int]
    max_players: int
    miniapp_url: str | None = None
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    webapp_dev_mode: bool = False

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        _load_env_file(Path(env_file))

        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "Не задан BOT_TOKEN. Скопируйте .env.example в .env "
                "и вставьте токен от @BotFather."
            )

        raw_admin_ids = os.getenv("ADMIN_IDS", "")
        try:
            admin_ids = frozenset(
                int(value.strip())
                for value in raw_admin_ids.split(",")
                if value.strip()
            )
        except ValueError as error:
            raise RuntimeError("ADMIN_IDS должен содержать Telegram ID через запятую.") from error

        database_path = Path(
            os.getenv("DATABASE_PATH", "data/table_tennis.sqlite3")
        ).expanduser()
        max_players = int(os.getenv("MAX_PLAYERS", "32"))
        if not 2 <= max_players <= 64:
            raise RuntimeError("MAX_PLAYERS должен быть от 2 до 64.")

        miniapp_url = os.getenv("MINIAPP_URL", "").strip().rstrip("/") or None
        if miniapp_url and not miniapp_url.startswith("https://"):
            raise RuntimeError("MINIAPP_URL должен начинаться с https://.")
        web_host = os.getenv("WEB_HOST", "127.0.0.1").strip()
        web_port = int(os.getenv("WEB_PORT", "8080"))
        if not 1 <= web_port <= 65535:
            raise RuntimeError("WEB_PORT должен быть от 1 до 65535.")
        webapp_dev_mode = os.getenv("WEBAPP_DEV_MODE", "0").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        return cls(
            bot_token=token,
            database_path=database_path,
            admin_ids=admin_ids,
            max_players=max_players,
            miniapp_url=miniapp_url,
            web_host=web_host,
            web_port=web_port,
            webapp_dev_mode=webapp_dev_mode,
        )
