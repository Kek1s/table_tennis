from __future__ import annotations

from html import escape
from math import ceil

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from table_tennis_bot.bracket import round_title
from table_tennis_bot.database import Database


STATUS_LABELS = {
    "registration": "📝 Набор игроков",
    "active": "🏓 Идёт турнир",
    "finished": "🏆 Завершён",
}

FORMAT_LABELS = {
    "single_elimination": "Олимпийская",
    "double_elimination": "Double Elimination",
    "round_robin": "Круговой турнир",
}

BRACKET_LABELS = {
    "winners": "Верхняя сетка",
    "losers": "Нижняя сетка",
    "grand_final": "Гранд-финал",
}


def _button_text(value: str, limit: int = 60) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fit_lines(lines: list[str], limit: int = 3900) -> str:
    result = "\n".join(lines)
    if len(result) <= limit:
        return result

    visible_lines: list[str] = []
    visible_length = 0
    for line in lines:
        line_length = len(line) + 1
        if visible_length + line_length > limit - 100:
            break
        visible_lines.append(line)
        visible_length += line_length
    visible_lines.extend(["", "…Список сокращён из-за лимита Telegram."])
    return "\n".join(visible_lines)


def main_menu(miniapp_url: str | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(
                text="📱 Открыть Mini App",
                web_app=WebAppInfo(url=miniapp_url),
            )
        ]
        if miniapp_url
        else [],
        [
            KeyboardButton(text="🏓 Турниры"),
            KeyboardButton(text="➕ Создать турнир"),
        ],
        [
            KeyboardButton(text="✏️ Моё имя"),
            KeyboardButton(text="ℹ️ Помощь"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[row for row in keyboard if row],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def tournaments_keyboard(tournaments: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tournament in tournaments:
        status_icon = {
            "registration": "📝",
            "active": "🏓",
            "finished": "🏆",
        }[tournament["status"]]
        builder.button(
            text=_button_text(f"{status_icon} {tournament['name']}"),
            callback_data=f"t:{tournament['id']}",
        )
    builder.adjust(1)
    return builder.as_markup()


def tournament_keyboard(
    database: Database,
    tournament,
    *,
    can_manage: bool,
    miniapp_url: str | None = None,
) -> InlineKeyboardMarkup:
    tournament_id = tournament["id"]
    builder = InlineKeyboardBuilder()

    if miniapp_url:
        separator = "&" if "?" in miniapp_url else "?"
        builder.row(
            InlineKeyboardButton(
                text="✨ Открыть красивую сетку",
                web_app=WebAppInfo(
                    url=f"{miniapp_url}{separator}tournament={tournament_id}"
                ),
            )
        )

    builder.button(
        text=f"👥 Участники ({tournament['player_count']})",
        callback_data=f"tp:{tournament_id}",
    )

    if tournament["status"] == "registration" and can_manage:
        builder.button(
            text="➕ Из зарегистрированных",
            callback_data=f"tae:{tournament_id}:0",
        )
        builder.button(
            text="➕ Добавить вручную",
            callback_data=f"tag:{tournament_id}",
        )
        if tournament["player_count"] >= 2:
            builder.button(
                text="🎲 Сформировать сетку",
                callback_data=f"tsc:{tournament_id}",
            )

    if tournament["status"] == "active" and can_manage:
        ready_matches = [
            match
            for match in database.list_matches(tournament_id)
            if match["status"] == "ready"
        ]
        for match in ready_matches:
            builder.button(
                text=_button_text(
                    f"✅ Матч: {match['player1_name']} — "
                    f"{match['player2_name']}"
                ),
                callback_data=f"tm:{match['id']}",
            )

    builder.button(text="🔄 Обновить", callback_data=f"t:{tournament_id}")
    builder.button(text="⬅️ Ко всем турнирам", callback_data="tl")
    builder.adjust(1)
    return builder.as_markup()


def participants_keyboard(
    tournament_id: int,
    players: list,
    *,
    can_manage: bool,
    registration_open: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_manage and registration_open:
        for player in players:
            builder.button(
                text=_button_text(f"➖ {player['display_name']}"),
                callback_data=f"trpc:{tournament_id}:{player['id']}",
            )
    builder.button(text="⬅️ К турниру", callback_data=f"t:{tournament_id}")
    builder.adjust(1)
    return builder.as_markup()


def available_players_keyboard(
    tournament_id: int,
    players: list,
    *,
    page: int,
    total_count: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for player in players:
        username = f" (@{player['username']})" if player["username"] else ""
        builder.button(
            text=_button_text(f"➕ {player['display_name']}{username}"),
            callback_data=f"tap:{tournament_id}:{player['id']}:{page}",
        )
    builder.adjust(1)

    page_count = max(1, ceil(total_count / page_size))
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"tae:{tournament_id}:{page - 1}",
            )
        )
    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{page_count}",
            callback_data="noop",
        )
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"tae:{tournament_id}:{page + 1}",
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К турниру",
            callback_data=f"t:{tournament_id}",
        )
    )
    return builder.as_markup()


def render_tournament(database: Database, tournament) -> str:
    tournament_name = escape(tournament["name"])
    status = STATUS_LABELS[tournament["status"]]
    tournament_format = FORMAT_LABELS.get(tournament["format"], tournament["format"])
    text = [
        f"<b>🏓 {tournament_name}</b>",
        f"{status} · {tournament_format} · участников: {tournament['player_count']}",
    ]

    if tournament["status"] == "registration":
        text.extend(
            [
                "",
                "Добавьте игроков и сформируйте турнирную сетку.",
            ]
        )
        return "\n".join(text)

    if tournament["status"] == "finished":
        text.extend(
            [
                "",
                f"<b>🏆 Победитель: {escape(tournament['champion_name'])}</b>",
            ]
        )

    matches = database.list_matches(tournament["id"])
    if not matches:
        return "\n".join(text)

    if tournament["format"] == "round_robin":
        standings = database.get_standings(tournament["id"])
        if standings:
            text.extend(["", "<b>Таблица</b>"])
            for row in standings:
                text.append(
                    f"{row['rank']}. {escape(row['display_name'])} · "
                    f"{row['wins']} побед"
                )

    rounds_count = max(match["round_number"] for match in matches)
    current_round = None
    current_bracket = None
    for match in matches:
        round_changed = match["round_number"] != current_round
        bracket_changed = match["bracket"] != current_bracket
        if round_changed or (
            tournament["format"] == "double_elimination" and bracket_changed
        ):
            current_round = match["round_number"]
            current_bracket = match["bracket"]
            if tournament["format"] == "single_elimination":
                heading = round_title(current_round, rounds_count)
            elif tournament["format"] == "round_robin":
                heading = f"Тур {current_round}"
            else:
                bracket_name = BRACKET_LABELS.get(
                    current_bracket,
                    current_bracket,
                )
                heading = f"Этап {current_round} · {bracket_name}"
            text.extend(
                [
                    "",
                    f"<b>{heading}</b>",
                ]
            )
        text.append(_render_match_line(match))

    return _fit_lines(text)


def render_participants(tournament, players: list) -> str:
    title = escape(tournament["name"])
    lines = [f"<b>👥 Участники · {title}</b>", ""]
    if not players:
        lines.append("Пока никого нет.")
    else:
        for index, player in enumerate(players, start=1):
            is_authorized = (
                player["telegram_id"] is not None and not bool(player["is_test"])
            )
            marker = "🤖" if is_authorized else "👤"
            rating = f" · рейтинг {player['rating']}" if is_authorized else ""
            lines.append(
                f"{index}. {marker} {escape(player['display_name'])}{rating}"
            )
    lines.extend(
        [
            "",
            "🤖 — авторизован через Telegram, участвует в рейтинге",
            "👤 — гость без рейтинга",
        ]
    )
    return _fit_lines(lines)


def _render_match_line(match) -> str:
    p1 = escape(match["player1_name"] or "ожидается")
    p2 = escape(match["player2_name"] or "ожидается")
    if match["status"] == "ready":
        return f"⚔️ {p1} — {p2}"
    if match["status"] == "finished":
        winner = escape(match["winner_name"])
        return f"✅ {p1} — {p2} → <b>{winner}</b>"
    if match["status"] == "bye":
        return f"↪️ {p1} проходит без игры"
    if match["player1_id"] or match["player2_id"]:
        known = p1 if match["player1_id"] else p2
        return f"⏳ {known} ждёт соперника"
    return "🔒 Ожидаются победители предыдущих матчей"
