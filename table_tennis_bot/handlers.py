from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from table_tennis_bot.config import Settings
from table_tennis_bot.database import Database
from table_tennis_bot.ui import (
    FORMAT_LABELS,
    available_players_keyboard,
    main_menu,
    participants_keyboard,
    render_participants,
    render_tournament,
    tournament_keyboard,
    tournaments_keyboard,
)


PAGE_SIZE = 8


class TournamentCreation(StatesGroup):
    waiting_for_name = State()
    waiting_for_format = State()


class GuestCreation(StatesGroup):
    waiting_for_name = State()


class PlayerRename(StatesGroup):
    waiting_for_name = State()


def create_router(database: Database, settings: Settings) -> Router:
    router = Router(name=__name__)
    reply_menu = main_menu(settings.miniapp_url)

    def ensure_registered(message: Message) -> None:
        if not message.from_user:
            return
        database.register_telegram_player(
            message.from_user.id,
            message.from_user.full_name,
            message.from_user.username,
        )

    def can_manage(tournament, telegram_id: int) -> bool:
        return (
            tournament["creator_telegram_id"] == telegram_id
            or telegram_id in settings.admin_ids
        )

    def tournament_markup(tournament, *, manager: bool) -> InlineKeyboardMarkup:
        return tournament_keyboard(
            database,
            tournament,
            can_manage=manager,
            miniapp_url=settings.miniapp_url,
        )

    async def safely_edit(
        callback: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if not isinstance(callback.message, Message):
            return
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    async def show_tournament(callback: CallbackQuery, tournament_id: int) -> bool:
        tournament = database.get_tournament(tournament_id)
        if not tournament:
            return False
        markup = tournament_markup(
            tournament,
            manager=can_manage(tournament, callback.from_user.id),
        )
        await safely_edit(
            callback,
            render_tournament(database, tournament),
            markup,
        )
        return True

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        ensure_registered(message)
        player = database.get_player_by_telegram_id(message.from_user.id)
        await message.answer(
            (
                f"Привет, <b>{escape(player['display_name'])}</b>!\n\n"
                "Здесь можно проводить турниры по настольному теннису "
                "в форматах Single Elimination, Double Elimination и "
                "кругового турнира.\n\n"
                "Вы уже зарегистрированы и доступны администраторам "
                "при добавлении в турнир."
            ),
            reply_markup=reply_menu,
        )

    @router.message(Command("help"))
    @router.message(F.text == "ℹ️ Помощь")
    async def help_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        ensure_registered(message)
        await message.answer(
            (
                "<b>Как провести турнир</b>\n\n"
                "1. Нажмите «Создать турнир» и введите название.\n"
                "2. Выберите формат и добавьте игроков.\n"
                "3. Сформируйте сетку.\n"
                "4. После игры откройте матч и выберите победителя.\n"
                "5. Сетка и турнирная таблица обновятся автоматически.\n\n"
                "<b>Команды</b>\n"
                "/start — регистрация и главное меню\n"
                "/app — открыть Mini App\n"
                "/tournaments — список турниров\n"
                "/new_tournament — новый турнир\n"
                "/rename — изменить своё имя\n"
                "/cancel — отменить текущий ввод"
            ),
            reply_markup=reply_menu,
        )

    @router.message(Command("app"))
    async def open_miniapp(message: Message) -> None:
        ensure_registered(message)
        if not settings.miniapp_url:
            await message.answer(
                "Mini App уже работает локально, но для открытия внутри "
                "Telegram нужно задать публичный HTTPS-адрес в MINIAPP_URL."
            )
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✨ Открыть турнирную сетку",
                        web_app=WebAppInfo(url=settings.miniapp_url),
                    )
                ]
            ]
        )
        await message.answer(
            "Откройте Mini App для создания турниров и просмотра сеток.",
            reply_markup=markup,
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=reply_menu)

    @router.message(Command("rename"))
    @router.message(F.text == "✏️ Моё имя")
    async def rename_begin(message: Message, state: FSMContext) -> None:
        ensure_registered(message)
        await state.set_state(PlayerRename.waiting_for_name)
        await message.answer(
            "Введите имя, которое будет показано в турнирной сетке.\n"
            "Для отмены: /cancel"
        )

    @router.message(PlayerRename.waiting_for_name)
    async def rename_finish(message: Message, state: FSMContext) -> None:
        if not message.text or not message.from_user:
            await message.answer("Отправьте имя обычным текстом.")
            return
        try:
            database.rename_telegram_player(message.from_user.id, message.text)
        except (ValueError, LookupError) as error:
            await message.answer(str(error))
            return
        await state.clear()
        player = database.get_player_by_telegram_id(message.from_user.id)
        await message.answer(
            f"Готово. Теперь ваше имя: <b>{escape(player['display_name'])}</b>",
            reply_markup=reply_menu,
        )

    @router.message(Command("new_tournament"))
    @router.message(F.text == "➕ Создать турнир")
    async def tournament_create_begin(message: Message, state: FSMContext) -> None:
        ensure_registered(message)
        await state.set_state(TournamentCreation.waiting_for_name)
        await message.answer(
            "Введите название турнира, например «Кубок офиса — июль».\n"
            "Для отмены: /cancel"
        )

    @router.message(TournamentCreation.waiting_for_name)
    async def tournament_create_finish(message: Message, state: FSMContext) -> None:
        if not message.text or not message.from_user:
            await message.answer("Отправьте название обычным текстом.")
            return
        tournament_name = " ".join(message.text.split())
        if not 2 <= len(tournament_name) <= 50:
            await message.answer("Название должно содержать от 2 до 50 символов.")
            return
        await state.update_data(tournament_name=tournament_name)
        await state.set_state(TournamentCreation.waiting_for_format)
        format_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏆 Single Elimination",
                        callback_data="nfmt:single_elimination",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="♻️ Double Elimination",
                        callback_data="nfmt:double_elimination",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔁 Круговой турнир",
                        callback_data="nfmt:round_robin",
                    )
                ],
            ]
        )
        await message.answer(
            "<b>Выберите формат турнира</b>\n\n"
            "Single — одно поражение.\n"
            "Double — выбывание после двух поражений.\n"
            "Круговой — каждый играет с каждым.",
            reply_markup=format_keyboard,
        )

    @router.callback_query(
        TournamentCreation.waiting_for_format,
        F.data.startswith("nfmt:"),
    )
    async def tournament_format_finish(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        tournament_format = callback.data.split(":", 1)[1]
        if tournament_format not in FORMAT_LABELS:
            await callback.answer("Неизвестный формат.", show_alert=True)
            return
        state_data = await state.get_data()
        tournament_name = state_data.get("tournament_name")
        if not tournament_name:
            await state.clear()
            await callback.answer("Создание турнира отменено.", show_alert=True)
            return
        tournament_id = database.create_tournament(
            tournament_name,
            callback.from_user.id,
            tournament_format,
        )
        await state.clear()
        tournament = database.get_tournament(tournament_id)
        await safely_edit(
            callback,
            render_tournament(database, tournament),
            tournament_markup(tournament, manager=True),
        )
        await callback.answer("Турнир создан.")

    @router.message(Command("tournaments"))
    @router.message(F.text == "🏓 Турниры")
    async def list_tournaments_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        ensure_registered(message)
        tournaments = database.list_tournaments()
        if not tournaments:
            await message.answer(
                "Турниров пока нет. Создайте первый!",
                reply_markup=reply_menu,
            )
            return
        await message.answer(
            "<b>🏓 Турниры</b>\nВыберите турнир:",
            reply_markup=tournaments_keyboard(tournaments),
        )

    @router.callback_query(F.data == "tl")
    async def list_tournaments_callback(callback: CallbackQuery) -> None:
        tournaments = database.list_tournaments()
        text = (
            "<b>🏓 Турниры</b>\nВыберите турнир:"
            if tournaments
            else "Турниров пока нет."
        )
        markup = tournaments_keyboard(tournaments) if tournaments else None
        await safely_edit(callback, text, markup)
        await callback.answer()

    @router.callback_query(F.data.startswith("t:"))
    async def tournament_open(callback: CallbackQuery) -> None:
        tournament_id = int(callback.data.split(":")[1])
        if await show_tournament(callback, tournament_id):
            await callback.answer()
        else:
            await callback.answer("Турнир не найден.", show_alert=True)

    @router.callback_query(F.data.startswith("tp:"))
    async def participants_open(callback: CallbackQuery) -> None:
        tournament_id = int(callback.data.split(":")[1])
        tournament = database.get_tournament(tournament_id)
        if not tournament:
            await callback.answer("Турнир не найден.", show_alert=True)
            return
        players = database.list_tournament_players(tournament_id)
        await safely_edit(
            callback,
            render_participants(tournament, players),
            participants_keyboard(
                tournament_id,
                players,
                can_manage=can_manage(tournament, callback.from_user.id),
                registration_open=tournament["status"] == "registration",
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tae:"))
    async def available_players_open(callback: CallbackQuery) -> None:
        _, raw_tournament_id, raw_page = callback.data.split(":")
        tournament_id = int(raw_tournament_id)
        page = max(0, int(raw_page))
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if tournament["status"] != "registration":
            await callback.answer("Турнир уже начался.", show_alert=True)
            return

        total_count = database.count_available_registered_players(tournament_id)
        players = database.list_available_registered_players(
            tournament_id,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
        text = (
            "<b>Зарегистрированные игроки</b>\n"
            "Нажмите на игрока, чтобы добавить его в турнир."
            if players
            else (
                "<b>Нет доступных игроков</b>\n\n"
                "Попросите игроков сначала открыть бота и нажать /start "
                "или добавьте их вручную."
            )
        )
        await safely_edit(
            callback,
            text,
            available_players_keyboard(
                tournament_id,
                players,
                page=page,
                total_count=total_count,
                page_size=PAGE_SIZE,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tap:"))
    async def available_player_add(callback: CallbackQuery) -> None:
        _, raw_tournament_id, raw_player_id, raw_page = callback.data.split(":")
        tournament_id = int(raw_tournament_id)
        player_id = int(raw_player_id)
        page = int(raw_page)
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            added = database.add_existing_player(
                tournament_id,
                player_id,
                max_players=settings.max_players,
            )
        except (ValueError, LookupError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer("Игрок добавлен." if added else "Игрок уже в турнире.")
        total_count = database.count_available_registered_players(tournament_id)
        max_page = max(0, (total_count - 1) // PAGE_SIZE)
        page = min(page, max_page)
        players = database.list_available_registered_players(
            tournament_id,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
        await safely_edit(
            callback,
            (
                "<b>Зарегистрированные игроки</b>\n"
                "Нажмите на игрока, чтобы добавить его в турнир."
                if players
                else "<b>Все зарегистрированные игроки уже добавлены.</b>"
            ),
            available_players_keyboard(
                tournament_id,
                players,
                page=page,
                total_count=total_count,
                page_size=PAGE_SIZE,
            ),
        )

    @router.callback_query(F.data.startswith("tag:"))
    async def guest_add_begin(callback: CallbackQuery, state: FSMContext) -> None:
        tournament_id = int(callback.data.split(":")[1])
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if tournament["status"] != "registration":
            await callback.answer("Турнир уже начался.", show_alert=True)
            return
        await state.set_state(GuestCreation.waiting_for_name)
        await state.update_data(tournament_id=tournament_id)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Введите имя игрока, которого нужно добавить вручную.\n"
                "Для отмены: /cancel"
            )
        await callback.answer()

    @router.message(GuestCreation.waiting_for_name)
    async def guest_add_finish(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Отправьте имя обычным текстом.")
            return
        state_data = await state.get_data()
        tournament_id = int(state_data["tournament_id"])
        tournament = database.get_tournament(tournament_id)
        if (
            not tournament
            or not message.from_user
            or not can_manage(tournament, message.from_user.id)
        ):
            await state.clear()
            await message.answer("Турнир не найден или недостаточно прав.")
            return
        try:
            database.add_guest_player(
                tournament_id,
                message.text,
                max_players=settings.max_players,
            )
        except (ValueError, LookupError) as error:
            await message.answer(str(error))
            return
        await state.clear()
        tournament = database.get_tournament(tournament_id)
        await message.answer(
            "Игрок добавлен.\n\n" + render_tournament(database, tournament),
            reply_markup=tournament_markup(tournament, manager=True),
        )

    @router.callback_query(F.data.startswith("trpc:"))
    async def remove_player_confirm(callback: CallbackQuery) -> None:
        _, raw_tournament_id, raw_player_id = callback.data.split(":")
        tournament_id = int(raw_tournament_id)
        player_id = int(raw_player_id)
        tournament = database.get_tournament(tournament_id)
        players = database.list_tournament_players(tournament_id)
        player = next((item for item in players if item["id"] == player_id), None)
        if (
            not tournament
            or not player
            or not can_manage(tournament, callback.from_user.id)
        ):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, удалить",
                        callback_data=f"trp:{tournament_id}:{player_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"tp:{tournament_id}",
                    )
                ],
            ]
        )
        await safely_edit(
            callback,
            (
                f"Удалить <b>{escape(player['display_name'])}</b> "
                f"из турнира «{escape(tournament['name'])}»?"
            ),
            markup,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("trp:"))
    async def remove_player(callback: CallbackQuery) -> None:
        _, raw_tournament_id, raw_player_id = callback.data.split(":")
        tournament_id = int(raw_tournament_id)
        player_id = int(raw_player_id)
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            removed = database.remove_player(tournament_id, player_id)
        except (ValueError, LookupError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer("Игрок удалён." if removed else "Игрок уже удалён.")
        await show_tournament(callback, tournament_id)

    @router.callback_query(F.data.startswith("tsc:"))
    async def tournament_start_confirm(callback: CallbackQuery) -> None:
        tournament_id = int(callback.data.split(":")[1])
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, сформировать сетку",
                        callback_data=f"ts:{tournament_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"t:{tournament_id}",
                    )
                ],
            ]
        )
        await safely_edit(
            callback,
            (
                "<b>Начать турнир?</b>\n\n"
                f"Формат: {FORMAT_LABELS[tournament['format']]}. "
                "После старта добавлять и удалять игроков нельзя."
            ),
            markup,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ts:"))
    async def tournament_start(callback: CallbackQuery) -> None:
        tournament_id = int(callback.data.split(":")[1])
        tournament = database.get_tournament(tournament_id)
        if not tournament or not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            database.start_tournament(tournament_id)
        except (ValueError, LookupError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer("Сетка сформирована!")
        await show_tournament(callback, tournament_id)

    @router.callback_query(F.data.startswith("tm:"))
    async def match_open(callback: CallbackQuery) -> None:
        match_id = int(callback.data.split(":")[1])
        match = database.get_match(match_id)
        if not match:
            await callback.answer("Матч не найден.", show_alert=True)
            return
        tournament = database.get_tournament(match["tournament_id"])
        if not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if match["status"] != "ready":
            await callback.answer("Матч уже завершён или ещё не готов.", show_alert=True)
            await show_tournament(callback, match["tournament_id"])
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🏆 {match['player1_name']}",
                        callback_data=f"twc:{match_id}:{match['player1_id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"🏆 {match['player2_name']}",
                        callback_data=f"twc:{match_id}:{match['player2_id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К сетке",
                        callback_data=f"t:{match['tournament_id']}",
                    )
                ],
            ]
        )
        await safely_edit(
            callback,
            (
                "<b>Кто выиграл матч?</b>\n\n"
                f"{escape(match['player1_name'])} — "
                f"{escape(match['player2_name'])}"
            ),
            markup,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("twc:"))
    async def winner_confirm(callback: CallbackQuery) -> None:
        _, raw_match_id, raw_winner_id = callback.data.split(":")
        match_id = int(raw_match_id)
        winner_id = int(raw_winner_id)
        match = database.get_match(match_id)
        if not match:
            await callback.answer("Матч не найден.", show_alert=True)
            return
        tournament = database.get_tournament(match["tournament_id"])
        if not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if (
            match["status"] != "ready"
            or winner_id not in (match["player1_id"], match["player2_id"])
        ):
            await callback.answer(
                "Матч уже завершён или выбран неверный игрок.",
                show_alert=True,
            )
            return
        winner_name = (
            match["player1_name"]
            if winner_id == match["player1_id"]
            else match["player2_name"]
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, записать победу",
                        callback_data=f"tw:{match_id}:{winner_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"tm:{match_id}",
                    )
                ],
            ]
        )
        await safely_edit(
            callback,
            f"Подтвердить победу игрока <b>{escape(winner_name)}</b>?",
            markup,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tw:"))
    async def winner_record(callback: CallbackQuery) -> None:
        _, raw_match_id, raw_winner_id = callback.data.split(":")
        match_id = int(raw_match_id)
        winner_id = int(raw_winner_id)
        match = database.get_match(match_id)
        if not match:
            await callback.answer("Матч не найден.", show_alert=True)
            return
        tournament = database.get_tournament(match["tournament_id"])
        if not can_manage(tournament, callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            tournament_id = database.record_winner(match_id, winner_id)
        except (ValueError, LookupError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer("Результат записан.")
        await show_tournament(callback, tournament_id)

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    return router
