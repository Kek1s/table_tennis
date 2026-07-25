from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

from table_tennis_bot.config import Settings
from table_tennis_bot.database import (
    ESTABLISHED_K,
    INITIAL_RATING,
    PROVISIONAL_GAMES,
    PROVISIONAL_K,
    Database,
    TOURNAMENT_FORMATS,
)
from table_tennis_bot.ui import FORMAT_LABELS


WEB_ROOT = Path(__file__).with_name("web")
DATABASE_KEY = web.AppKey("database", Database)
SETTINGS_KEY = web.AppKey("settings", Settings)
USER_KEY = web.RequestKey("telegram_user", dict)


@dataclass(frozen=True, slots=True)
class TelegramWebUser:
    id: int
    display_name: str
    username: str | None


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
    now: int | None = None,
) -> TelegramWebUser:
    """Validate Telegram Mini App initData using the official HMAC flow."""
    if not init_data:
        raise PermissionError("Откройте приложение через Telegram.")

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise PermissionError("В данных Telegram отсутствует подпись.")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise PermissionError("Подпись Telegram не прошла проверку.")

    current_time = int(time.time()) if now is None else now
    try:
        auth_date = int(values["auth_date"])
    except (KeyError, ValueError) as error:
        raise PermissionError("Некорректная дата авторизации Telegram.") from error
    if auth_date > current_time + 300 or current_time - auth_date > max_age_seconds:
        raise PermissionError("Сессия Mini App устарела. Откройте её заново.")

    try:
        user_data = json.loads(values["user"])
        telegram_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError("Telegram не передал данные пользователя.") from error

    first_name = str(user_data.get("first_name", "")).strip()
    last_name = str(user_data.get("last_name", "")).strip()
    display_name = " ".join(part for part in (first_name, last_name) if part)
    if not display_name:
        display_name = f"Игрок {telegram_id}"
    username = user_data.get("username")
    return TelegramWebUser(
        id=telegram_id,
        display_name=display_name,
        username=str(username) if username else None,
    )


@web.middleware
async def error_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except PermissionError as error:
        response = web.json_response({"error": str(error)}, status=403)
    except LookupError as error:
        response = web.json_response({"error": str(error)}, status=404)
    except (ValueError, json.JSONDecodeError) as error:
        response = web.json_response({"error": str(error)}, status=400)
    except web.HTTPException:
        raise

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@web.middleware
async def telegram_auth_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    if not request.path.startswith("/api/") or request.path == "/api/health":
        return await handler(request)

    settings = request.app[SETTINGS_KEY]
    database = request.app[DATABASE_KEY]
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if settings.webapp_dev_mode and not init_data:
        raw_user_id = request.headers.get("X-Dev-User-Id")
        try:
            local_user_id = int(raw_user_id or "900000001")
        except ValueError as error:
            raise PermissionError("Некорректный тестовый пользователь.") from error
        existing_player = (
            database.get_player_by_telegram_id(local_user_id)
            if raw_user_id
            else database.get_local_authorized_player(settings.admin_ids)
        )
        if existing_player and not existing_player["is_test"]:
            user = TelegramWebUser(
                id=int(existing_player["telegram_id"]),
                display_name=existing_player["display_name"],
                username=existing_player["username"],
            )
            player = existing_player
        else:
            user = TelegramWebUser(
                id=local_user_id,
                display_name="Локальный администратор",
                username="local_admin",
            )
            player = database.register_telegram_player(
                user.id,
                user.display_name,
                user.username,
                is_test=True,
            )
    else:
        user = validate_telegram_init_data(init_data, settings.bot_token)
        player = database.register_telegram_player(
            user.id,
            user.display_name,
            user.username,
        )

    request[USER_KEY] = {
        "id": int(player["telegram_id"]),
        "display_name": player["display_name"],
        "username": player["username"],
        "rating": player["rating"],
        "rated_games": int(player["rated_games"]),
        "is_authorized": not bool(player["is_test"]),
    }
    return await handler(request)


def _can_manage(tournament, user_id: int, settings: Settings) -> bool:
    return (
        tournament["creator_telegram_id"] == user_id
        or user_id in settings.admin_ids
    )


def _tournament_summary(tournament, user_id: int, settings: Settings) -> dict[str, Any]:
    return {
        "id": int(tournament["id"]),
        "name": tournament["name"],
        "format": tournament["format"],
        "format_label": FORMAT_LABELS.get(
            tournament["format"],
            tournament["format"],
        ),
        "status": tournament["status"],
        "player_count": int(tournament["player_count"]),
        "champion_player_id": tournament["champion_player_id"],
        "champion_name": tournament["champion_name"],
        "can_manage": _can_manage(tournament, user_id, settings),
        "created_at": tournament["created_at"],
    }


def _tournament_payload(
    database: Database,
    settings: Settings,
    tournament_id: int,
    user_id: int,
) -> dict[str, Any]:
    tournament = database.get_tournament(tournament_id)
    if not tournament:
        raise LookupError("Турнир не найден.")

    players = [
        {
            "id": int(player["id"]),
            "display_name": player["display_name"],
            "telegram_id": player["telegram_id"],
            "username": player["username"],
            "is_authorized": (
                player["telegram_id"] is not None and not bool(player["is_test"])
            ),
            "rating": player["rating"],
            "rated_games": int(player["rated_games"]),
            "rating_delta": player["tournament_rating_delta"],
            "tournament_rated_games": player["tournament_rated_games"],
            "seed": int(player["seed"]),
            "losses": int(player["losses"]),
        }
        for player in database.list_tournament_players(tournament_id)
    ]
    matches = [
        {
            "id": int(match["id"]),
            "round_number": int(match["round_number"]),
            "position": int(match["position"]),
            "bracket_round": int(match["bracket_round"]),
            "bracket_position": int(match["bracket_position"]),
            "bracket": match["bracket"],
            "player1_id": match["player1_id"],
            "player1_name": match["player1_name"],
            "player2_id": match["player2_id"],
            "player2_name": match["player2_name"],
            "winner_id": match["winner_id"],
            "winner_name": match["winner_name"],
            "status": match["status"],
        }
        for match in database.list_matches(tournament_id)
    ]
    return {
        "tournament": _tournament_summary(tournament, user_id, settings),
        "players": players,
        "matches": matches,
        "standings": (
            database.get_standings(tournament_id)
            if tournament["format"] == "round_robin"
            else []
        ),
    }


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        WEB_ROOT / "index.html",
        headers={"Cache-Control": "no-store"},
    )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def bootstrap(request: web.Request) -> web.Response:
    database = request.app[DATABASE_KEY]
    settings = request.app[SETTINGS_KEY]
    user = request[USER_KEY]
    tournaments = [
        _tournament_summary(tournament, user["id"], settings)
        for tournament in database.list_tournaments(limit=100)
    ]
    ratings = [
        {
            "rank": rank,
            "player_id": int(player["id"]),
            "telegram_id": int(player["telegram_id"]),
            "display_name": player["display_name"],
            "username": player["username"],
            "rating": int(player["rating"]),
            "rated_games": int(player["rated_games"]),
            "is_current": int(player["telegram_id"]) == user["id"],
        }
        for rank, player in enumerate(database.list_rating(limit=100), start=1)
    ]
    return web.json_response(
        {
            "user": user,
            "tournaments": tournaments,
            "ratings": ratings,
            "rating_system": {
                "name": "Tournament Elo",
                "initial": INITIAL_RATING,
                "provisional_games": PROVISIONAL_GAMES,
                "provisional_k": PROVISIONAL_K,
                "established_k": ESTABLISHED_K,
            },
            "formats": [
                {"id": format_id, "label": FORMAT_LABELS[format_id]}
                for format_id in (
                    "single_elimination",
                    "double_elimination",
                    "round_robin",
                )
            ],
            "max_players": settings.max_players,
        }
    )


async def tournament_detail(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    return web.json_response(
        _tournament_payload(
            request.app[DATABASE_KEY],
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def delete_tournament(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    _managed_tournament(request, tournament_id)
    if not request.app[DATABASE_KEY].delete_tournament(tournament_id):
        raise LookupError("Турнир не найден.")
    return web.json_response({"deleted": True, "tournament_id": tournament_id})


async def create_tournament(request: web.Request) -> web.Response:
    body = await request.json()
    tournament_format = str(body.get("format", ""))
    if tournament_format not in TOURNAMENT_FORMATS:
        raise ValueError("Выберите формат турнира.")
    tournament_id = request.app[DATABASE_KEY].create_tournament(
        str(body.get("name", "")),
        request[USER_KEY]["id"],
        tournament_format,
    )
    return web.json_response(
        _tournament_payload(
            request.app[DATABASE_KEY],
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        ),
        status=201,
    )


def _managed_tournament(request: web.Request, tournament_id: int):
    tournament = request.app[DATABASE_KEY].get_tournament(tournament_id)
    if not tournament:
        raise LookupError("Турнир не найден.")
    if not _can_manage(
        tournament,
        request[USER_KEY]["id"],
        request.app[SETTINGS_KEY],
    ):
        raise PermissionError("Управлять турниром может только администратор.")
    return tournament


async def available_players(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    _managed_tournament(request, tournament_id)
    database = request.app[DATABASE_KEY]
    players = database.list_available_registered_players(
        tournament_id,
        limit=100,
        offset=0,
    )
    return web.json_response(
        {
            "players": [
                {
                    "id": int(player["id"]),
                    "display_name": player["display_name"],
                    "username": player["username"],
                }
                for player in players
            ]
        }
    )


async def add_player(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    _managed_tournament(request, tournament_id)
    body = await request.json()
    database = request.app[DATABASE_KEY]
    if body.get("player_id") is not None:
        database.add_existing_player(
            tournament_id,
            int(body["player_id"]),
            max_players=request.app[SETTINGS_KEY].max_players,
        )
    else:
        database.add_guest_player(
            tournament_id,
            str(body.get("display_name", "")),
            max_players=request.app[SETTINGS_KEY].max_players,
        )
    return web.json_response(
        _tournament_payload(
            database,
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def remove_player(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    player_id = int(request.match_info["player_id"])
    _managed_tournament(request, tournament_id)
    request.app[DATABASE_KEY].remove_player(tournament_id, player_id)
    return web.json_response(
        _tournament_payload(
            request.app[DATABASE_KEY],
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def start_tournament(request: web.Request) -> web.Response:
    tournament_id = int(request.match_info["tournament_id"])
    _managed_tournament(request, tournament_id)
    request.app[DATABASE_KEY].start_tournament(tournament_id)
    return web.json_response(
        _tournament_payload(
            request.app[DATABASE_KEY],
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def record_winner(request: web.Request) -> web.Response:
    match_id = int(request.match_info["match_id"])
    database = request.app[DATABASE_KEY]
    match = database.get_match(match_id)
    if not match:
        raise LookupError("Матч не найден.")
    _managed_tournament(request, int(match["tournament_id"]))
    body = await request.json()
    if body.get("winner_id") is None:
        raise ValueError("Выберите победителя матча.")
    tournament_id = database.record_winner(match_id, int(body["winner_id"]))
    return web.json_response(
        _tournament_payload(
            database,
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def change_winner(request: web.Request) -> web.Response:
    match_id = int(request.match_info["match_id"])
    database = request.app[DATABASE_KEY]
    match = database.get_match(match_id)
    if not match:
        raise LookupError("Матч не найден.")
    _managed_tournament(request, int(match["tournament_id"]))
    body = await request.json()
    if body.get("winner_id") is None:
        raise ValueError("Выберите победителя матча.")
    tournament_id = database.change_winner(match_id, int(body["winner_id"]))
    return web.json_response(
        _tournament_payload(
            database,
            request.app[SETTINGS_KEY],
            tournament_id,
            request[USER_KEY]["id"],
        )
    )


async def rename_profile(request: web.Request) -> web.Response:
    body = await request.json()
    database = request.app[DATABASE_KEY]
    database.rename_telegram_player(
        request[USER_KEY]["id"],
        str(body.get("display_name", "")),
    )
    player = database.get_player_by_telegram_id(request[USER_KEY]["id"])
    return web.json_response(
        {
            "user": {
                "id": int(player["telegram_id"]),
                "display_name": player["display_name"],
                "username": player["username"],
                "rating": player["rating"],
                "rated_games": int(player["rated_games"]),
                "is_authorized": not bool(player["is_test"]),
            }
        }
    )


def create_web_application(database: Database, settings: Settings) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, telegram_auth_middleware],
        client_max_size=64 * 1024,
    )
    app[DATABASE_KEY] = database
    app[SETTINGS_KEY] = settings
    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/tournaments", create_tournament)
    app.router.add_get("/api/tournaments/{tournament_id:\\d+}", tournament_detail)
    app.router.add_delete(
        "/api/tournaments/{tournament_id:\\d+}",
        delete_tournament,
    )
    app.router.add_get(
        "/api/tournaments/{tournament_id:\\d+}/available-players",
        available_players,
    )
    app.router.add_post(
        "/api/tournaments/{tournament_id:\\d+}/players",
        add_player,
    )
    app.router.add_delete(
        "/api/tournaments/{tournament_id:\\d+}/players/{player_id:\\d+}",
        remove_player,
    )
    app.router.add_post(
        "/api/tournaments/{tournament_id:\\d+}/start",
        start_tournament,
    )
    app.router.add_post("/api/matches/{match_id:\\d+}/winner", record_winner)
    app.router.add_patch("/api/matches/{match_id:\\d+}/winner", change_winner)
    app.router.add_patch("/api/profile", rename_profile)
    app.router.add_static("/assets/", WEB_ROOT, show_index=False)
    return app


async def start_web_server(
    database: Database,
    settings: Settings,
) -> web.AppRunner:
    runner = web.AppRunner(create_web_application(database, settings))
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    return runner
