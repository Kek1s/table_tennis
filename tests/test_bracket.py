import random
import unittest

from table_tennis_bot.bracket import (
    bracket_size,
    first_round_pairs,
    round_robin_rounds,
    round_title,
    total_rounds,
)


class BracketTests(unittest.TestCase):
    def test_bracket_size_is_next_power_of_two(self) -> None:
        self.assertEqual(bracket_size(2), 2)
        self.assertEqual(bracket_size(3), 4)
        self.assertEqual(bracket_size(5), 8)
        self.assertEqual(bracket_size(8), 8)
        self.assertEqual(bracket_size(17), 32)

    def test_requires_at_least_two_players(self) -> None:
        with self.assertRaises(ValueError):
            bracket_size(1)
        with self.assertRaises(ValueError):
            first_round_pairs([1])

    def test_pairs_contain_every_player_once_and_no_empty_matches(self) -> None:
        for player_count in range(2, 33):
            players = list(range(1, player_count + 1))
            pairs = first_round_pairs(players, rng=random.Random(42))
            flattened = [player for pair in pairs for player in pair if player is not None]

            self.assertEqual(len(pairs), bracket_size(player_count) // 2)
            self.assertEqual(sorted(flattened), players)
            self.assertTrue(all(pair[0] is not None for pair in pairs))

    def test_round_titles(self) -> None:
        self.assertEqual(total_rounds(8), 3)
        self.assertEqual(round_title(1, 3), "Четвертьфинал")
        self.assertEqual(round_title(2, 3), "Полуфинал")
        self.assertEqual(round_title(3, 3), "Финал")

    def test_round_robin_contains_each_pair_once(self) -> None:
        for player_count in range(2, 13):
            players = list(range(1, player_count + 1))
            rounds = round_robin_rounds(players)
            real_pairs = [
                frozenset((first, second))
                for round_pairs in rounds
                for first, second in round_pairs
                if second is not None
            ]
            expected_pairs = {
                frozenset((first, second))
                for first in players
                for second in players
                if first < second
            }
            self.assertEqual(set(real_pairs), expected_pairs)
            self.assertEqual(len(real_pairs), len(expected_pairs))


if __name__ == "__main__":
    unittest.main()
