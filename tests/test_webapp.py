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


if __name__ == "__main__":
    unittest.main()

