from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Protocol, TypeVar


T = TypeVar("T")


class Shuffler(Protocol):
    def shuffle(self, values: list[T]) -> None: ...


def bracket_size(player_count: int) -> int:
    """Return the smallest power of two that can contain all players."""
    if player_count < 2:
        raise ValueError("Для старта турнира нужны минимум 2 игрока.")
    return 1 << (player_count - 1).bit_length()


def total_rounds(player_count: int) -> int:
    return int(math.log2(bracket_size(player_count)))


def first_round_pairs(
    player_ids: Sequence[T],
    *,
    rng: Shuffler | None = None,
) -> list[tuple[T, T | None]]:
    """
    Shuffle players and distribute byes across first-round matches.

    Every match receives at least one player. This prevents empty branches and
    lets a bye automatically advance its player to the next round.
    """
    if len(player_ids) < 2:
        raise ValueError("Для старта турнира нужны минимум 2 игрока.")

    shuffled = list(player_ids)
    (rng or random.SystemRandom()).shuffle(shuffled)

    size = bracket_size(len(shuffled))
    match_count = size // 2
    pairs: list[tuple[T, T | None]] = []
    for index in range(match_count):
        opponent_index = match_count + index
        opponent = (
            shuffled[opponent_index]
            if opponent_index < len(shuffled)
            else None
        )
        pairs.append((shuffled[index], opponent))
    return pairs


def round_title(round_number: int, rounds_count: int) -> str:
    remaining = rounds_count - round_number
    if remaining == 0:
        return "Финал"
    if remaining == 1:
        return "Полуфинал"
    if remaining == 2:
        return "Четвертьфинал"
    return f"1/{2 ** remaining} финала"


def round_robin_rounds(
    player_ids: Sequence[T],
) -> list[list[tuple[T, T | None]]]:
    """Build a balanced round-robin schedule with one match per pair."""
    if len(player_ids) < 2:
        raise ValueError("Для старта турнира нужны минимум 2 игрока.")

    rotation: list[T | None] = list(player_ids)
    if len(rotation) % 2:
        rotation.append(None)

    rounds: list[list[tuple[T, T | None]]] = []
    for _ in range(len(rotation) - 1):
        pairs: list[tuple[T, T | None]] = []
        half = len(rotation) // 2
        for index in range(half):
            first = rotation[index]
            second = rotation[-(index + 1)]
            if first is None:
                first, second = second, None
            if first is not None:
                pairs.append((first, second))
        rounds.append(pairs)
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]
    return rounds
