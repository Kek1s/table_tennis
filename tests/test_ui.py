import random
import tempfile
import unittest
from pathlib import Path

from table_tennis_bot.database import Database
from table_tennis_bot.ui import (
    participants_keyboard,
    render_participants,
    render_tournament,
)


class UiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_large_tournament_fits_telegram_limits(self) -> None:
        self.database.register_telegram_player(1, "Администратор", "admin")
        tournament_id = self.database.create_tournament("Большой & кубок", 1)
        for index in range(64):
            self.database.add_guest_player(
                tournament_id,
                f"{index + 1:02d} " + "&" * 45,
                max_players=64,
            )

        tournament = self.database.get_tournament(tournament_id)
        players = self.database.list_tournament_players(tournament_id)
        participants_text = render_participants(tournament, players)
        keyboard = participants_keyboard(
            tournament_id,
            players,
            can_manage=True,
            registration_open=True,
        )

        self.assertLessEqual(len(participants_text), 3900)
        self.assertLessEqual(
            sum(len(row) for row in keyboard.inline_keyboard),
            100,
        )

        self.database.start_tournament(
            tournament_id,
            rng=random.Random(42),
        )
        tournament = self.database.get_tournament(tournament_id)
        bracket_text = render_tournament(self.database, tournament)
        self.assertLessEqual(len(bracket_text), 3900)


if __name__ == "__main__":
    unittest.main()

