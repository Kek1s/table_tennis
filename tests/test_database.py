import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from table_tennis_bot.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_tournament_with_players(
        self,
        player_count: int,
        tournament_format: str = "single_elimination",
    ) -> int:
        creator_id = 100
        self.database.register_telegram_player(creator_id, "Администратор", "admin")
        tournament_id = self.database.create_tournament(
            "Кубок офиса",
            creator_id,
            tournament_format,
        )
        for index in range(player_count):
            self.database.add_guest_player(
                tournament_id,
                f"Игрок {index + 1}",
                max_players=32,
            )
        return tournament_id

    def test_registration_keeps_custom_display_name(self) -> None:
        self.database.register_telegram_player(10, "Первое имя", "old_username")
        self.database.rename_telegram_player(10, "Имя для сетки")
        self.database.register_telegram_player(10, "Имя из Telegram", "new_username")

        player = self.database.get_player_by_telegram_id(10)
        self.assertEqual(player["display_name"], "Имя для сетки")
        self.assertEqual(player["username"], "new_username")

    def test_existing_database_is_migrated_for_new_formats(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE players (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                display_name TEXT NOT NULL,
                username TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE tournaments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                creator_telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                champion_player_id INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE tournament_players (
                tournament_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                seed INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (tournament_id, player_id),
                UNIQUE (tournament_id, seed)
            );
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY,
                tournament_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                position INTEGER NOT NULL,
                player1_id INTEGER,
                player2_id INTEGER,
                winner_id INTEGER,
                status TEXT NOT NULL,
                next_match_id INTEGER,
                next_slot INTEGER,
                UNIQUE (tournament_id, round_number, position)
            );
            """
        )
        connection.close()

        legacy_database = Database(legacy_path)
        legacy_database.initialize()
        with legacy_database.connect() as migrated:
            tournament_columns = {
                row["name"]
                for row in migrated.execute(
                    "PRAGMA table_info(tournaments)"
                ).fetchall()
            }
            participant_columns = {
                row["name"]
                for row in migrated.execute(
                    "PRAGMA table_info(tournament_players)"
                ).fetchall()
            }
            match_columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(matches)").fetchall()
            }
        self.assertIn("format", tournament_columns)
        self.assertIn("losses", participant_columns)
        self.assertIn("bracket", match_columns)

    def test_existing_player_cannot_be_added_twice(self) -> None:
        self.database.register_telegram_player(1, "Админ", "admin")
        player = self.database.register_telegram_player(2, "Игрок", "player")
        tournament_id = self.database.create_tournament("Тестовый турнир", 1)

        self.assertTrue(
            self.database.add_existing_player(
                tournament_id,
                player["id"],
                max_players=8,
            )
        )
        self.assertFalse(
            self.database.add_existing_player(
                tournament_id,
                player["id"],
                max_players=8,
            )
        )
        self.assertEqual(len(self.database.list_tournament_players(tournament_id)), 1)

    def test_five_player_tournament_advances_byes_and_finishes(self) -> None:
        tournament_id = self._create_tournament_with_players(5)
        self.database.start_tournament(tournament_id, rng=random.Random(7))

        matches = self.database.list_matches(tournament_id)
        self.assertEqual(len(matches), 7)
        self.assertEqual(sum(match["status"] == "bye" for match in matches), 3)
        self.assertEqual(sum(match["status"] == "ready" for match in matches), 2)

        recorded_results = 0
        while self.database.get_tournament(tournament_id)["status"] == "active":
            ready_matches = [
                match
                for match in self.database.list_matches(tournament_id)
                if match["status"] == "ready"
            ]
            self.assertTrue(ready_matches)
            match = ready_matches[0]
            self.database.record_winner(match["id"], match["player1_id"])
            recorded_results += 1

        tournament = self.database.get_tournament(tournament_id)
        self.assertEqual(tournament["status"], "finished")
        self.assertIsNotNone(tournament["champion_player_id"])
        self.assertEqual(recorded_results, 4)

    def test_players_cannot_change_after_start(self) -> None:
        tournament_id = self._create_tournament_with_players(2)
        self.database.start_tournament(tournament_id, rng=random.Random(1))

        with self.assertRaises(ValueError):
            self.database.add_guest_player(
                tournament_id,
                "Опоздавший",
                max_players=8,
            )

    def test_common_tournament_sizes_reach_a_champion(self) -> None:
        for player_count in (2, 3, 4, 6, 8, 9, 16, 17):
            with self.subTest(player_count=player_count):
                tournament_id = self._create_tournament_with_players(player_count)
                participant_ids = {
                    player["id"]
                    for player in self.database.list_tournament_players(tournament_id)
                }
                self.database.start_tournament(
                    tournament_id,
                    rng=random.Random(player_count),
                )

                recorded_results = 0
                while self.database.get_tournament(tournament_id)["status"] == "active":
                    ready_matches = [
                        match
                        for match in self.database.list_matches(tournament_id)
                        if match["status"] == "ready"
                    ]
                    self.assertTrue(ready_matches)
                    match = ready_matches[0]
                    self.database.record_winner(match["id"], match["player1_id"])
                    recorded_results += 1

                tournament = self.database.get_tournament(tournament_id)
                self.assertIn(tournament["champion_player_id"], participant_ids)
                self.assertEqual(recorded_results, player_count - 1)

    def test_only_participant_can_be_recorded_as_winner(self) -> None:
        tournament_id = self._create_tournament_with_players(2)
        self.database.start_tournament(tournament_id, rng=random.Random(1))
        match = self.database.list_matches(tournament_id)[0]

        with self.assertRaises(ValueError):
            self.database.record_winner(match["id"], 999_999)

    def test_double_elimination_requires_two_losses(self) -> None:
        for player_count in (2, 3, 5, 8):
            with self.subTest(player_count=player_count):
                tournament_id = self._create_tournament_with_players(
                    player_count,
                    "double_elimination",
                )
                self.database.start_tournament(
                    tournament_id,
                    rng=random.Random(player_count),
                )
                results = 0
                while self.database.get_tournament(tournament_id)["status"] == "active":
                    ready_matches = [
                        match
                        for match in self.database.list_matches(tournament_id)
                        if match["status"] == "ready"
                    ]
                    self.assertTrue(ready_matches)
                    match = ready_matches[0]
                    self.database.record_winner(match["id"], match["player1_id"])
                    results += 1

                tournament = self.database.get_tournament(tournament_id)
                players = self.database.list_tournament_players(tournament_id)
                champion = next(
                    player
                    for player in players
                    if player["id"] == tournament["champion_player_id"]
                )
                eliminated = [player for player in players if player["id"] != champion["id"]]
                self.assertLess(champion["losses"], 2)
                self.assertTrue(all(player["losses"] == 2 for player in eliminated))
                self.assertEqual(results, 2 * (player_count - 1))

    def test_round_robin_plays_every_pair_and_builds_standings(self) -> None:
        player_count = 5
        tournament_id = self._create_tournament_with_players(
            player_count,
            "round_robin",
        )
        self.database.start_tournament(tournament_id)

        recorded_results = 0
        while self.database.get_tournament(tournament_id)["status"] == "active":
            ready_matches = [
                match
                for match in self.database.list_matches(tournament_id)
                if match["status"] == "ready"
            ]
            self.assertTrue(ready_matches)
            for match in ready_matches:
                self.database.record_winner(match["id"], match["player1_id"])
                recorded_results += 1

        standings = self.database.get_standings(tournament_id)
        self.assertEqual(recorded_results, player_count * (player_count - 1) // 2)
        self.assertEqual(len(standings), player_count)
        self.assertEqual(sum(row["played"] for row in standings), recorded_results * 2)
        self.assertIsNotNone(
            self.database.get_tournament(tournament_id)["champion_player_id"]
        )


if __name__ == "__main__":
    unittest.main()
