from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from table_tennis_bot.config import Settings
from table_tennis_bot.database import Database
from table_tennis_bot.handlers import create_router
from table_tennis_bot.webapp import start_web_server

logger = logging.getLogger(__name__)


async def _configure_bot(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню и регистрация"),
            BotCommand(command="app", description="Открыть Mini App"),
            BotCommand(command="tournaments", description="Список турниров"),
            BotCommand(command="new_tournament", description="Создать турнир"),
            BotCommand(command="rename", description="Изменить своё имя"),
            BotCommand(command="help", description="Инструкция"),
            BotCommand(command="cancel", description="Отменить ввод"),
        ]
    )
    if settings.miniapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Турниры",
                web_app=WebAppInfo(url=settings.miniapp_url),
            )
        )


async def _start_bot_with_retry(
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
) -> None:
    retry_delay = 2
    while True:
        try:
            await _configure_bot(bot, settings)
            await dispatcher.start_polling(bot, close_bot_session=False)
            return
        except TelegramNetworkError as error:
            logger.warning(
                "Нет соединения с Telegram API: %s. Повтор через %s сек.",
                error,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


async def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    web_runner = await start_web_server(database, settings)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(database, settings))

    try:
        await _start_bot_with_retry(bot, dispatcher, settings)
    finally:
        await bot.session.close()
        await web_runner.cleanup()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(main())


if __name__ == "__main__":
    run()
