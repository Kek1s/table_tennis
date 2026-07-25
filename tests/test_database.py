import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from table_tennis_bot.bracket import bracket_size
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
            base_player_columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(players)").fetchall()
            }
            rating_history_exists = migrated.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'rating_history'
                """
            ).fetchone()
        self.assertIn("format", tournament_columns)
        self.assertIn("losses", participant_columns)
        self.assertIn("bracket", match_columns)
        self.assertIn("bracket_version", tournament_columns)
        self.assertIn("loser_next_match_id", match_columns)
        self.assertIn("loser_next_slot", match_columns)
        self.assertIn("bracket_round", match_columns)
        self.assertIn("bracket_position", match_columns)
        self.assertIn("rating", base_player_columns)
        self.assertIn("rated_games", base_player_columns)
        self.assertIn("is_test", base_player_columns)
        self.assertIsNotNone(rating_history_exists)

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

    def test_tournament_can_be_deleted_with_matches_and_guest_players(self) -> None:
        tournament_id = self._create_tournament_with_players(3)
        guest_ids = {
            player["id"]
            for player in self.database.list_tournament_players(tournament_id)
            if player["telegram_id"] is None
        }
        self.database.start_tournament(tournament_id, rng=random.Random(2))

        self.assertTrue(self.database.delete_tournament(tournament_id))
        self.assertIsNone(self.database.get_tournament(tournament_id))
        self.assertEqual(self.database.list_matches(tournament_id), [])
        with self.database.connect() as connection:
            remaining_guests = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM players
                WHERE id IN ({",".join("?" for _ in guest_ids)})
                """,
                tuple(guest_ids),
            ).fetchone()
        self.assertEqual(remaining_guests["count"], 0)
        self.assertFalse(self.database.delete_tournament(tournament_id))

    def test_rating_is_recalculated_after_result_change_and_deletion(self) -> None:
        first = self.database.register_telegram_player(501, "Анна", "anna")
        second = self.database.register_telegram_player(502, "Борис", "boris")
        tournament_id = self.database.create_tournament("Рейтинговый", 501)
        for player in (first, second):
            self.database.add_existing_player(
                tournament_id,
                player["id"],
                max_players=8,
            )
        self.database.start_tournament(tournament_id, rng=random.Random(1))
        match = self.database.list_matches(tournament_id)[0]
        self.database.record_winner(match["id"], match["player1_id"])

        winner = self.database.get_match(match["id"])["winner_id"]
        loser = (
            match["player2_id"]
            if winner == match["player1_id"]
            else match["player1_id"]
        )
        with self.database.connect() as connection:
            winner_row = connection.execute(
                "SELECT rating, rated_games FROM players WHERE id = ?",
                (winner,),
            ).fetchone()
            loser_row = connection.execute(
                "SELECT rating, rated_games FROM players WHERE id = ?",
                (loser,),
            ).fetchone()
        self.assertEqual(winner_row["rating"], 1520)
        self.assertEqual(loser_row["rating"], 1480)
        self.assertEqual(winner_row["rated_games"], 1)

        self.database.change_winner(match["id"], loser)
        with self.database.connect() as connection:
            corrected_winner = connection.execute(
                "SELECT rating FROM players WHERE id = ?",
                (loser,),
            ).fetchone()
            corrected_loser = connection.execute(
                "SELECT rating FROM players WHERE id = ?",
                (winner,),
            ).fetchone()
            history = connection.execute(
                """
                SELECT COUNT(*) AS count FROM rating_history
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchone()
        self.assertEqual(corrected_winner["rating"], 1520)
        self.assertEqual(corrected_loser["rating"], 1480)
        self.assertEqual(history["count"], 2)

        self.database.delete_tournament(tournament_id)
        ratings = self.database.list_rating()
        self.assertEqual({row["rating"] for row in ratings}, {1500})
        self.assertEqual({row["rated_games"] for row in ratings}, {0})

    def test_guests_and_local_test_profile_do_not_have_rating(self) -> None:
        authorized = self.database.register_telegram_player(
            601,
            "Авторизованный",
            "authorized",
        )
        local = self.database.register_telegram_player(
            900000001,
            "Локальный администратор",
            "local_admin",
            is_test=True,
        )
        tournament_id = self.database.create_tournament("С гостем", 601)
        self.database.add_existing_player(
            tournament_id,
            authorized["id"],
            max_players=8,
        )
        guest_id = self.database.add_guest_player(
            tournament_id,
            "Гость",
            max_players=8,
        )
        self.database.start_tournament(tournament_id, rng=random.Random(1))
        match = self.database.list_matches(tournament_id)[0]
        self.database.record_winner(match["id"], match["player1_id"])

        self.assertIsNone(local["rating"])
        self.assertNotIn(
            local["id"],
            {player["id"] for player in self.database.list_rating()},
        )
        self.assertEqual(
            self.database.get_player_by_telegram_id(601)["rating"],
            1500,
        )
        players = self.database.list_tournament_players(tournament_id)
        guest = next(player for player in players if player["id"] == guest_id)
        self.assertIsNone(guest["rating"])

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

    def test_single_result_can_be_corrected_without_erasing_later_results(self) -> None:
        tournament_id = self._create_tournament_with_players(4)
        self.database.start_tournament(tournament_id, rng=random.Random(4))
        first_round = [
            match
            for match in self.database.list_matches(tournament_id)
            if match["round_number"] == 1
        ]
        corrected_match = first_round[0]
        sibling_match = first_round[1]
        self.database.record_winner(
            corrected_match["id"],
            corrected_match["player1_id"],
        )
        self.database.change_winner(
            corrected_match["id"],
            corrected_match["player2_id"],
        )
        corrected = self.database.get_match(corrected_match["id"])
        self.assertEqual(corrected["winner_id"], corrected_match["player2_id"])

        self.database.record_winner(
            sibling_match["id"],
            sibling_match["player1_id"],
        )
        final = next(
            match
            for match in self.database.list_matches(tournament_id)
            if match["status"] == "ready"
        )
        self.assertIn(
            corrected_match["player2_id"],
            (final["player1_id"], final["player2_id"]),
        )
        self.database.record_winner(final["id"], final["player1_id"])
        champion_before = self.database.get_tournament(tournament_id)[
            "champion_player_id"
        ]

        with self.assertRaisesRegex(ValueError, "сыгранный следующий матч"):
            self.database.change_winner(
                corrected_match["id"],
                corrected_match["player1_id"],
            )
        self.assertEqual(
            self.database.get_tournament(tournament_id)["champion_player_id"],
            champion_before,
        )
        self.assertEqual(self.database.get_match(final["id"])["status"], "finished")

    def test_double_elimination_has_one_decisive_grand_final(self) -> None:
        for player_count in (2, 3, 4, 5, 6, 8, 9, 16, 17, 32):
            with self.subTest(player_count=player_count):
                tournament_id = self._create_tournament_with_players(
                    player_count,
                    "double_elimination",
                )
                self.database.start_tournament(
                    tournament_id,
                    rng=random.Random(player_count),
                )
                initial_matches = self.database.list_matches(tournament_id)
                self.assertEqual(
                    len(initial_matches),
                    2 * bracket_size(player_count) - 2,
                )
                self.assertEqual(
                    self.database.get_tournament(tournament_id)["bracket_version"],
                    2,
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
                    winner_id = (
                        match["player2_id"]
                        if match["bracket"] == "grand_final"
                        else match["player1_id"]
                    )
                    self.database.record_winner(match["id"], winner_id)
                    results += 1

                tournament = self.database.get_tournament(tournament_id)
                players = self.database.list_tournament_players(tournament_id)
                champion = next(
                    player
                    for player in players
                    if player["id"] == tournament["champion_player_id"]
                )
                matches = self.database.list_matches(tournament_id)
                self.assertEqual(len(matches), len(initial_matches))
                grand_finals = [
                    match for match in matches if match["bracket"] == "grand_final"
                ]
                self.assertEqual(len(grand_finals), 1)
                self.assertEqual(grand_finals[0]["winner_id"], champion["id"])
                self.assertFalse(
                    any(
                        match["bracket"] == "grand_final_reset"
                        for match in matches
                    )
                )
                self.assertLessEqual(champion["losses"], 1)
                self.assertEqual(results, 2 * (player_count - 1))
            self.assertFalse(
                any(
                    match["status"] == "bye"
                    and match["player1_id"] is not None
                    and match["player2_id"] is not None
                    for match in matches
                )
            )
            self.assertFalse(
                any(
                    match["status"] == "bye"
                    and match["player1_id"] is None
                    and match["player2_id"] is not None
                    for match in matches
                )
            )

    def test_fixed_double_bracket_has_one_source_for_every_slot(self) -> None:
        tournament_id = self._create_tournament_with_players(
            16,
            "double_elimination",
        )
        self.database.start_tournament(tournament_id, rng=random.Random(16))
        with self.database.connect() as connection:
            matches = connection.execute(
                """
                SELECT * FROM matches WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchall()
            for match in matches:
                if match["bracket"] == "winners" and match["bracket_round"] == 1:
                    continue
                for slot in (1, 2):
                    feeders = connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM matches
                        WHERE tournament_id = ?
                          AND (
                            (next_match_id = ? AND next_slot = ?)
                            OR
                            (loser_next_match_id = ? AND loser_next_slot = ?)
                          )
                        """,
                        (
                            tournament_id,
                            match["id"],
                            slot,
                            match["id"],
                            slot,
                        ),
                    ).fetchone()
                    self.assertEqual(
                        feeders["count"],
                        1,
                        (match["bracket"], match["bracket_round"], slot),
                    )

    def test_double_result_correction_never_erases_played_later_matches(self) -> None:
        tournament_id = self._create_tournament_with_players(
            4,
            "double_elimination",
        )
        self.database.start_tournament(tournament_id, rng=random.Random(8))
        stage_one = [
            match
            for match in self.database.list_matches(tournament_id)
            if match["round_number"] == 1 and match["status"] == "ready"
        ]
        for match in stage_one:
            self.database.record_winner(match["id"], match["player1_id"])

        corrected_match = stage_one[0]
        self.database.change_winner(
            corrected_match["id"],
            corrected_match["player2_id"],
        )
        self.assertEqual(
            self.database.get_match(corrected_match["id"])["winner_id"],
            corrected_match["player2_id"],
        )
        next_stage = [
            match
            for match in self.database.list_matches(tournament_id)
            if match["round_number"] == 2 and match["status"] == "ready"
        ]
        self.assertTrue(next_stage)
        played_later = next_stage[0]
        self.database.record_winner(played_later["id"], played_later["player1_id"])

        with self.assertRaisesRegex(ValueError, "сыгранный следующий матч"):
            self.database.change_winner(
                corrected_match["id"],
                corrected_match["player1_id"],
            )
        self.assertEqual(
            self.database.get_match(played_later["id"])["status"],
            "finished",
        )

    def test_round_robin_winner_can_be_corrected(self) -> None:
        tournament_id = self._create_tournament_with_players(2, "round_robin")
        self.database.start_tournament(tournament_id)
        match = self.database.list_matches(tournament_id)[0]
        self.database.record_winner(match["id"], match["player1_id"])
        self.assertEqual(
            self.database.get_tournament(tournament_id)["champion_player_id"],
            match["player1_id"],
        )

        self.database.change_winner(match["id"], match["player2_id"])

        self.assertEqual(
            self.database.get_tournament(tournament_id)["champion_player_id"],
            match["player2_id"],
        )
        self.assertEqual(
            self.database.get_match(match["id"])["winner_id"],
            match["player2_id"],
        )

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
