import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from table_tennis_bot.config import Settings
from table_tennis_bot.database import Database
from table_tennis_bot.webapp import (
    create_web_application,
    validate_telegram_init_data,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebAssetTests(unittest.TestCase):
    def test_sheet_buttons_reach_the_delegated_click_handler(self) -> None:
        app_js = (PROJECT_ROOT / "table_tennis_bot" / "web" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("data-sheet-backdrop", app_js)
        self.assertIn("event.target === sheetBackdrop", app_js)
        self.assertNotIn('onclick="event.stopPropagation()"', app_js)
        self.assertIn('["winners", "grand_final"]', app_js)


class TelegramAuthTests(unittest.TestCase):
    def test_valid_init_data_is_accepted_and_tampering_is_rejected(self) -> None:
        token = "123456789:secret-token"
        now = 2_000_000_000
        values = {
            "auth_date": str(now),
            "query_id": "query-1",
            "user": json.dumps(
                {
                    "id": 777,
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "username": "ivan",
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        secret_key = hmac.new(
            b"WebAppData",
            token.encode(),
            hashlib.sha256,
        ).digest()
        values["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        init_data = urlencode(values)

        user = validate_telegram_init_data(init_data, token, now=now)
        self.assertEqual(user.id, 777)
        self.assertEqual(user.display_name, "Иван Петров")

        with self.assertRaises(PermissionError):
            validate_telegram_init_data(
                init_data.replace("ivan", "hacker"),
                token,
                now=now,
            )


class WebAppApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "api.sqlite3")
        self.database.initialize()
        self.settings = Settings(
            bot_token="123456789:test-token",
            database_path=self.database.path,
            admin_ids=frozenset(),
            max_players=32,
            webapp_dev_mode=True,
        )
        app = create_web_application(self.database, self.settings)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_create_and_run_double_elimination_via_api(self) -> None:
        response = await self.client.post(
            "/api/tournaments",
            json={"name": "Mini App Cup", "format": "double_elimination"},
        )
        self.assertEqual(response.status, 201)
        payload = await response.json()
        tournament_id = payload["tournament"]["id"]

        for name in ("Анна", "Борис", "Света"):
            response = await self.client.post(
                f"/api/tournaments/{tournament_id}/players",
                json={"display_name": name},
            )
            self.assertEqual(response.status, 200)

        response = await self.client.post(
            f"/api/tournaments/{tournament_id}/start",
            json={},
        )
        payload = await response.json()
        self.assertEqual(payload["tournament"]["status"], "active")
        self.assertEqual(payload["tournament"]["format"], "double_elimination")

        while payload["tournament"]["status"] == "active":
            ready = [
                match for match in payload["matches"] if match["status"] == "ready"
            ]
            self.assertTrue(ready)
            match = ready[0]
            response = await self.client.post(
                f"/api/matches/{match['id']}/winner",
                json={"winner_id": match["player1_id"]},
            )
            self.assertEqual(response.status, 200)
            payload = await response.json()

        self.assertIsNotNone(payload["tournament"]["champion_player_id"])
        self.assertEqual(
            sum(match["bracket"] == "grand_final" for match in payload["matches"]),
            1,
        )
        self.assertFalse(
            any(
                match["bracket"] == "grand_final_reset"
                for match in payload["matches"]
            )
        )

    async def test_local_preview_reuses_authorized_telegram_player(self) -> None:
        self.database.register_telegram_player(777, "Настоящий игрок", "real")

        response = await self.client.get("/api/bootstrap")

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["user"]["id"], 777)
        self.assertTrue(payload["user"]["is_authorized"])
        self.assertEqual(payload["user"]["rating"], 1500)
        self.assertEqual(len(payload["ratings"]), 1)
        with self.database.connect() as connection:
            players = connection.execute(
                "SELECT telegram_id, is_test FROM players"
            ).fetchall()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["telegram_id"], 777)

    async def test_result_correction_and_tournament_deletion_via_api(self) -> None:
        response = await self.client.post(
            "/api/tournaments",
            json={"name": "Исправляемый кубок", "format": "single_elimination"},
        )
        payload = await response.json()
        tournament_id = payload["tournament"]["id"]
        for name in ("Лена", "Маша"):
            response = await self.client.post(
                f"/api/tournaments/{tournament_id}/players",
                json={"display_name": name},
            )
            self.assertEqual(response.status, 200)

        response = await self.client.post(
            f"/api/tournaments/{tournament_id}/start",
            json={},
        )
        payload = await response.json()
        match = next(item for item in payload["matches"] if item["status"] == "ready")
        response = await self.client.post(
            f"/api/matches/{match['id']}/winner",
            json={"winner_id": match["player1_id"]},
        )
        self.assertEqual(response.status, 200)

        response = await self.client.patch(
            f"/api/matches/{match['id']}/winner",
            json={"winner_id": match["player2_id"]},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(
            payload["tournament"]["champion_player_id"],
            match["player2_id"],
        )

        response = await self.client.delete(f"/api/tournaments/{tournament_id}")
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["deleted"])
        response = await self.client.get(f"/api/tournaments/{tournament_id}")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
