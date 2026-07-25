from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

from table_tennis_bot.config import Settings
from table_tennis_bot.main import _start_bot_with_retry


class BotStartupTests(IsolatedAsyncioTestCase):
    async def test_retries_after_temporary_telegram_network_error(self) -> None:
        bot = object()
        network_error = TelegramNetworkError(
            method=GetMe(),
            message="temporary timeout",
        )
        dispatcher = SimpleNamespace(
            start_polling=AsyncMock(side_effect=[network_error, None])
        )
        settings = Settings(
            bot_token="test-token",
            database_path=Path("test.sqlite3"),
            admin_ids=frozenset(),
            max_players=32,
        )

        with (
            patch(
                "table_tennis_bot.main._configure_bot",
                new=AsyncMock(),
            ) as configure_bot,
            patch(
                "table_tennis_bot.main.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            await _start_bot_with_retry(bot, dispatcher, settings)

        self.assertEqual(configure_bot.await_count, 2)
        self.assertEqual(dispatcher.start_polling.await_count, 2)
        dispatcher.start_polling.assert_awaited_with(
            bot,
            close_bot_session=False,
        )
        sleep.assert_awaited_once_with(2)


if __name__ == "__main__":
    import unittest

    unittest.main()
