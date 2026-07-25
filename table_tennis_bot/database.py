from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from table_tennis_bot.bracket import (
    Shuffler,
    first_round_pairs,
    round_robin_rounds,
)


TOURNAMENT_FORMATS = frozenset(
    {
        "single_elimination",
        "double_elimination",
        "round_robin",
    }
)

INITIAL_RATING = 1500
PROVISIONAL_GAMES = 30
PROVISIONAL_K = 40
ESTABLISHED_K = 20


SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    display_name TEXT NOT NULL,
    username TEXT,
    rating INTEGER,
    rated_games INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    creator_telegram_id INTEGER NOT NULL,
    format TEXT NOT NULL DEFAULT 'single_elimination'
        CHECK (format IN ('single_elimination', 'double_elimination', 'round_robin')),
    status TEXT NOT NULL DEFAULT 'registration'
        CHECK (status IN ('registration', 'active', 'finished')),
    bracket_version INTEGER NOT NULL DEFAULT 2,
    champion_player_id INTEGER REFERENCES players(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS tournament_players (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    seed INTEGER NOT NULL,
    losses INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    PRIMARY KEY (tournament_id, player_id),
    UNIQUE (tournament_id, seed)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    position INTEGER NOT NULL,
    bracket TEXT NOT NULL DEFAULT 'main',
    player1_id INTEGER REFERENCES players(id),
    player2_id INTEGER REFERENCES players(id),
    winner_id INTEGER REFERENCES players(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'bye', 'finished')),
    next_match_id INTEGER REFERENCES matches(id),
    next_slot INTEGER CHECK (next_slot IN (1, 2)),
    loser_next_match_id INTEGER REFERENCES matches(id),
    loser_next_slot INTEGER CHECK (loser_next_slot IN (1, 2)),
    bracket_round INTEGER NOT NULL DEFAULT 1,
    bracket_position INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tournament_id, round_number, position)
);

CREATE TABLE IF NOT EXISTS rating_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    rating_before INTEGER NOT NULL,
    rating_after INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    games INTEGER NOT NULL,
    calculated_at TEXT NOT NULL,
    UNIQUE (tournament_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_tournaments_status
    ON tournaments(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_matches_tournament
    ON matches(tournament_id, round_number, position);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_name(value: str, *, label: str = "Имя") -> str:
    cleaned = " ".join(value.split())
    if not 2 <= len(cleaned) <= 50:
        raise ValueError(f"{label} должно содержать от 2 до 50 символов.")
    return cleaned


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._migrate_schema(connection)
            self._recalculate_all_ratings(connection)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        base_player_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(players)").fetchall()
        }
        if "rating" not in base_player_columns:
            connection.execute("ALTER TABLE players ADD COLUMN rating INTEGER")
        if "rated_games" not in base_player_columns:
            connection.execute(
                """
                ALTER TABLE players
                ADD COLUMN rated_games INTEGER NOT NULL DEFAULT 0
                """
            )
        if "is_test" not in base_player_columns:
            connection.execute(
                """
                ALTER TABLE players
                ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0
                """
            )
        connection.execute(
            """
            UPDATE players
            SET is_test = 1, rating = NULL, rated_games = 0
            WHERE telegram_id = 900000001
              AND username = 'local_admin'
              AND display_name = 'Локальный администратор'
            """
        )
        connection.execute(
            """
            UPDATE players
            SET rating = ?, rated_games = COALESCE(rated_games, 0)
            WHERE telegram_id IS NOT NULL AND is_test = 0 AND rating IS NULL
            """,
            (INITIAL_RATING,),
        )
        connection.execute(
            """
            UPDATE players SET rating = NULL, rated_games = 0
            WHERE telegram_id IS NULL OR is_test = 1
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_players_rating
            ON players(is_test, rating DESC)
            """
        )

        tournament_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tournaments)").fetchall()
        }
        if "format" not in tournament_columns:
            connection.execute(
                """
                ALTER TABLE tournaments
                ADD COLUMN format TEXT NOT NULL DEFAULT 'single_elimination'
                """
            )
        if "bracket_version" not in tournament_columns:
            connection.execute(
                """
                ALTER TABLE tournaments
                ADD COLUMN bracket_version INTEGER NOT NULL DEFAULT 1
                """
            )

        player_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(tournament_players)"
            ).fetchall()
        }
        if "losses" not in player_columns:
            connection.execute(
                """
                ALTER TABLE tournament_players
                ADD COLUMN losses INTEGER NOT NULL DEFAULT 0
                """
            )

        match_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(matches)").fetchall()
        }
        if "bracket" not in match_columns:
            connection.execute(
                """
                ALTER TABLE matches
                ADD COLUMN bracket TEXT NOT NULL DEFAULT 'main'
                """
            )
        if "loser_next_match_id" not in match_columns:
            connection.execute(
                """
                ALTER TABLE matches ADD COLUMN loser_next_match_id INTEGER
                REFERENCES matches(id)
                """
            )
        if "loser_next_slot" not in match_columns:
            connection.execute(
                """
                ALTER TABLE matches ADD COLUMN loser_next_slot INTEGER
                CHECK (loser_next_slot IN (1, 2))
                """
            )
        if "bracket_round" not in match_columns:
            connection.execute(
                """
                ALTER TABLE matches
                ADD COLUMN bracket_round INTEGER NOT NULL DEFAULT 1
                """
            )
            connection.execute(
                "UPDATE matches SET bracket_round = round_number"
            )
        if "bracket_position" not in match_columns:
            connection.execute(
                """
                ALTER TABLE matches
                ADD COLUMN bracket_position INTEGER NOT NULL DEFAULT 1
                """
            )
            connection.execute(
                "UPDATE matches SET bracket_position = position"
            )

        # Older versions could create a second grand-final match when the
        # lower-bracket winner won the first one. The current rules use one
        # decisive grand final, so collapse an existing reset into that match.
        reset_tournaments = connection.execute(
            """
            SELECT DISTINCT tournament_id FROM matches
            WHERE bracket = 'grand_final_reset'
            """
        ).fetchall()
        for row in reset_tournaments:
            tournament_id = int(row["tournament_id"])
            tournament = connection.execute(
                """
                SELECT champion_player_id FROM tournaments WHERE id = ?
                """,
                (tournament_id,),
            ).fetchone()
            grand_final = connection.execute(
                """
                SELECT id, winner_id FROM matches
                WHERE tournament_id = ? AND bracket = 'grand_final'
                ORDER BY round_number DESC LIMIT 1
                """,
                (tournament_id,),
            ).fetchone()
            reset = connection.execute(
                """
                SELECT winner_id FROM matches
                WHERE tournament_id = ? AND bracket = 'grand_final_reset'
                ORDER BY round_number DESC LIMIT 1
                """,
                (tournament_id,),
            ).fetchone()
            champion_id = (
                tournament["champion_player_id"]
                or reset["winner_id"]
                or (grand_final["winner_id"] if grand_final else None)
            )
            connection.execute(
                """
                DELETE FROM matches
                WHERE tournament_id = ? AND bracket = 'grand_final_reset'
                """,
                (tournament_id,),
            )
            if grand_final and champion_id is not None:
                connection.execute(
                    """
                    UPDATE matches
                    SET winner_id = ?, status = 'finished'
                    WHERE id = ?
                    """,
                    (champion_id, grand_final["id"]),
                )
                connection.execute(
                    """
                    UPDATE tournaments
                    SET status = 'finished', champion_player_id = ?,
                        finished_at = COALESCE(finished_at, ?)
                    WHERE id = ?
                    """,
                    (champion_id, _utc_now(), tournament_id),
                )

    def register_telegram_player(
        self,
        telegram_id: int,
        display_name: str,
        username: str | None,
        *,
        is_test: bool = False,
    ) -> sqlite3.Row:
        display_name = _clean_name(display_name)
        initial_rating = None if is_test else INITIAL_RATING
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO players
                    (telegram_id, display_name, username, rating, rated_games,
                     is_test, created_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    rating = CASE
                        WHEN excluded.is_test = 0
                        THEN COALESCE(players.rating, ?)
                        ELSE players.rating
                    END,
                    is_test = CASE
                        WHEN excluded.is_test = 0 THEN 0
                        ELSE players.is_test
                    END
                """,
                (
                    telegram_id,
                    display_name,
                    username,
                    initial_rating,
                    int(is_test),
                    _utc_now(),
                    INITIAL_RATING,
                ),
            )
            return connection.execute(
                "SELECT * FROM players WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

    def rename_telegram_player(self, telegram_id: int, display_name: str) -> None:
        display_name = _clean_name(display_name)
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE players SET display_name = ? WHERE telegram_id = ?",
                (display_name, telegram_id),
            )
            if cursor.rowcount == 0:
                raise LookupError("Игрок не зарегистрирован.")

    def get_player_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM players WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

    def get_local_authorized_player(
        self,
        preferred_telegram_ids: frozenset[int] = frozenset(),
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            if preferred_telegram_ids:
                placeholders = ",".join("?" for _ in preferred_telegram_ids)
                preferred = connection.execute(
                    f"""
                    SELECT * FROM players
                    WHERE telegram_id IN ({placeholders}) AND is_test = 0
                    ORDER BY created_at LIMIT 1
                    """,
                    tuple(preferred_telegram_ids),
                ).fetchone()
                if preferred:
                    return preferred
            return connection.execute(
                """
                SELECT * FROM players
                WHERE telegram_id IS NOT NULL AND is_test = 0
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()

    def list_rating(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id, telegram_id, display_name, username, rating, rated_games
                FROM players
                WHERE telegram_id IS NOT NULL AND is_test = 0
                  AND rating IS NOT NULL
                ORDER BY rating DESC, rated_games DESC,
                         display_name COLLATE NOCASE
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def create_tournament(
        self,
        name: str,
        creator_telegram_id: int,
        tournament_format: str = "single_elimination",
    ) -> int:
        name = _clean_name(name, label="Название")
        if tournament_format not in TOURNAMENT_FORMATS:
            raise ValueError("Неизвестный формат турнира.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tournaments
                    (name, creator_telegram_id, format, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, creator_telegram_id, tournament_format, _utc_now()),
            )
            return int(cursor.lastrowid)

    def get_tournament(self, tournament_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT t.*, p.display_name AS champion_name,
                       (SELECT COUNT(*) FROM tournament_players tp
                        WHERE tp.tournament_id = t.id) AS player_count
                FROM tournaments t
                LEFT JOIN players p ON p.id = t.champion_player_id
                WHERE t.id = ?
                """,
                (tournament_id,),
            ).fetchone()

    def list_tournaments(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT t.*, p.display_name AS champion_name,
                       (SELECT COUNT(*) FROM tournament_players tp
                        WHERE tp.tournament_id = t.id) AS player_count
                FROM tournaments t
                LEFT JOIN players p ON p.id = t.champion_player_id
                ORDER BY
                    CASE t.status
                        WHEN 'active' THEN 0
                        WHEN 'registration' THEN 1
                        ELSE 2
                    END,
                    t.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def delete_tournament(self, tournament_id: int) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guest_rows = connection.execute(
                """
                SELECT p.id
                FROM players p
                JOIN tournament_players tp ON tp.player_id = p.id
                WHERE tp.tournament_id = ? AND p.telegram_id IS NULL
                """,
                (tournament_id,),
            ).fetchall()
            cursor = connection.execute(
                "DELETE FROM tournaments WHERE id = ?",
                (tournament_id,),
            )
            if cursor.rowcount:
                for guest in guest_rows:
                    connection.execute(
                        """
                        DELETE FROM players
                        WHERE id = ? AND telegram_id IS NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM tournament_players
                              WHERE player_id = players.id
                          )
                        """,
                        (guest["id"],),
                    )
                self._recalculate_all_ratings(connection)
            return cursor.rowcount > 0

    def list_tournament_players(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT p.*, tp.seed, tp.losses,
                       rh.rating_before AS tournament_rating_before,
                       rh.rating_after AS tournament_rating_after,
                       rh.delta AS tournament_rating_delta,
                       rh.games AS tournament_rated_games
                FROM tournament_players tp
                JOIN players p ON p.id = tp.player_id
                LEFT JOIN rating_history rh
                  ON rh.tournament_id = tp.tournament_id
                 AND rh.player_id = tp.player_id
                WHERE tp.tournament_id = ?
                ORDER BY tp.seed
                """,
                (tournament_id,),
            ).fetchall()

    def add_existing_player(
        self,
        tournament_id: int,
        player_id: int,
        *,
        max_players: int,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_registration_open(connection, tournament_id)
            count = self._participant_count(connection, tournament_id)
            if count >= max_players:
                raise ValueError(f"В турнире уже максимум игроков: {max_players}.")

            player = connection.execute(
                "SELECT id FROM players WHERE id = ?",
                (player_id,),
            ).fetchone()
            if not player:
                raise LookupError("Игрок не найден.")

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tournament_players
                    (tournament_id, player_id, seed, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (tournament_id, player_id, count + 1, _utc_now()),
            )
            return cursor.rowcount > 0

    def add_guest_player(
        self,
        tournament_id: int,
        display_name: str,
        *,
        max_players: int,
    ) -> int:
        display_name = _clean_name(display_name)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_registration_open(connection, tournament_id)
            count = self._participant_count(connection, tournament_id)
            if count >= max_players:
                raise ValueError(f"В турнире уже максимум игроков: {max_players}.")

            cursor = connection.execute(
                """
                INSERT INTO players (telegram_id, display_name, username, created_at)
                VALUES (NULL, ?, NULL, ?)
                """,
                (display_name, _utc_now()),
            )
            player_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO tournament_players
                    (tournament_id, player_id, seed, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (tournament_id, player_id, count + 1, _utc_now()),
            )
            return player_id

    def remove_player(self, tournament_id: int, player_id: int) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_registration_open(connection, tournament_id)
            cursor = connection.execute(
                """
                DELETE FROM tournament_players
                WHERE tournament_id = ? AND player_id = ?
                """,
                (tournament_id, player_id),
            )
            if cursor.rowcount:
                rows = connection.execute(
                    """
                    SELECT player_id FROM tournament_players
                    WHERE tournament_id = ?
                    ORDER BY seed
                    """,
                    (tournament_id,),
                ).fetchall()
                for seed, row in enumerate(rows, start=1):
                    connection.execute(
                        """
                        UPDATE tournament_players SET seed = ?
                        WHERE tournament_id = ? AND player_id = ?
                        """,
                        (seed, tournament_id, row["player_id"]),
                    )
            return cursor.rowcount > 0

    def list_available_registered_players(
        self,
        tournament_id: int,
        *,
        limit: int,
        offset: int,
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT p.*
                FROM players p
                WHERE p.telegram_id IS NOT NULL AND p.is_test = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM tournament_players tp
                      WHERE tp.tournament_id = ? AND tp.player_id = p.id
                  )
                ORDER BY p.display_name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (tournament_id, limit, offset),
            ).fetchall()

    def count_available_registered_players(self, tournament_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM players p
                WHERE p.telegram_id IS NOT NULL AND p.is_test = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM tournament_players tp
                      WHERE tp.tournament_id = ? AND tp.player_id = p.id
                  )
                """,
                (tournament_id,),
            ).fetchone()
            return int(row["count"])

    def start_tournament(
        self,
        tournament_id: int,
        *,
        rng: Shuffler | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tournament = self._ensure_registration_open(connection, tournament_id)
            players = connection.execute(
                """
                SELECT player_id FROM tournament_players
                WHERE tournament_id = ?
                ORDER BY seed
                """,
                (tournament_id,),
            ).fetchall()
            player_ids = [int(row["player_id"]) for row in players]
            if len(player_ids) < 2:
                raise ValueError("Для старта турнира нужны минимум 2 игрока.")

            connection.execute(
                """
                UPDATE tournament_players SET losses = 0
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            )
            connection.execute(
                """
                UPDATE tournaments
                SET status = 'active', started_at = ?, bracket_version = 2
                WHERE id = ?
                """,
                (_utc_now(), tournament_id),
            )

            tournament_format = tournament["format"]
            if tournament_format == "single_elimination":
                self._start_single_elimination(
                    connection,
                    tournament_id,
                    player_ids,
                    rng=rng,
                )
            elif tournament_format == "double_elimination":
                self._start_double_elimination(
                    connection,
                    tournament_id,
                    player_ids,
                    rng=rng,
                )
            elif tournament_format == "round_robin":
                self._start_round_robin(connection, tournament_id, player_ids)
            else:
                raise ValueError("Неизвестный формат турнира.")

    def _start_single_elimination(
        self,
        connection: sqlite3.Connection,
        tournament_id: int,
        player_ids: list[int],
        *,
        rng: Shuffler | None,
    ) -> None:
        pairs = first_round_pairs(player_ids, rng=rng)
        match_count = len(pairs)
        rounds_count = (match_count * 2).bit_length() - 1
        match_ids: dict[tuple[int, int], int] = {}

        for round_number in range(1, rounds_count + 1):
            matches_in_round = 2 ** (rounds_count - round_number)
            for position in range(1, matches_in_round + 1):
                cursor = connection.execute(
                    """
                    INSERT INTO matches
                        (tournament_id, round_number, position, bracket, status)
                    VALUES (?, ?, ?, 'main', 'pending')
                    """,
                    (tournament_id, round_number, position),
                )
                match_ids[(round_number, position)] = int(cursor.lastrowid)

        for (round_number, position), match_id in match_ids.items():
            if round_number == rounds_count:
                continue
            next_position = (position + 1) // 2
            next_slot = 1 if position % 2 else 2
            connection.execute(
                """
                UPDATE matches SET next_match_id = ?, next_slot = ?
                WHERE id = ?
                """,
                (
                    match_ids[(round_number + 1, next_position)],
                    next_slot,
                    match_id,
                ),
            )

        for position, (player1_id, player2_id) in enumerate(pairs, start=1):
            match_id = match_ids[(1, position)]
            if player2_id is None:
                connection.execute(
                    """
                    UPDATE matches
                    SET player1_id = ?, winner_id = ?, status = 'bye'
                    WHERE id = ?
                    """,
                    (player1_id, player1_id, match_id),
                )
                self._place_winner_in_next_match(
                    connection,
                    match_id,
                    int(player1_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE matches
                    SET player1_id = ?, player2_id = ?, status = 'ready'
                    WHERE id = ?
                    """,
                    (player1_id, player2_id, match_id),
                )

    def _start_double_elimination(
        self,
        connection: sqlite3.Connection,
        tournament_id: int,
        player_ids: list[int],
        *,
        rng: Shuffler | None,
    ) -> None:
        pairs = first_round_pairs(player_ids, rng=rng)
        bracket_size = len(pairs) * 2
        winners_rounds = bracket_size.bit_length() - 1
        losers_rounds = max(0, 2 * winners_rounds - 2)
        stage_positions: dict[int, int] = {}
        winners: dict[tuple[int, int], int] = {}
        losers: dict[tuple[int, int], int] = {}

        def insert_match(
            *,
            bracket: str,
            bracket_round: int,
            bracket_position: int,
            stage: int,
        ) -> int:
            stage_positions[stage] = stage_positions.get(stage, 0) + 1
            cursor = connection.execute(
                """
                INSERT INTO matches
                    (tournament_id, round_number, position, bracket,
                     bracket_round, bracket_position, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    tournament_id,
                    stage,
                    stage_positions[stage],
                    bracket,
                    bracket_round,
                    bracket_position,
                ),
            )
            return int(cursor.lastrowid)

        for round_number in range(1, winners_rounds + 1):
            count = bracket_size // (2**round_number)
            stage = 1 if round_number == 1 else 2 * round_number - 2
            for position in range(1, count + 1):
                winners[(round_number, position)] = insert_match(
                    bracket="winners",
                    bracket_round=round_number,
                    bracket_position=position,
                    stage=stage,
                )

        for round_number in range(1, losers_rounds + 1):
            pair_level = (round_number + 1) // 2
            count = bracket_size // (2 ** (pair_level + 1))
            for position in range(1, count + 1):
                losers[(round_number, position)] = insert_match(
                    bracket="losers",
                    bracket_round=round_number,
                    bracket_position=position,
                    stage=round_number + 1,
                )

        grand_final_id = insert_match(
            bracket="grand_final",
            bracket_round=1,
            bracket_position=1,
            stage=2 * winners_rounds,
        )

        for (round_number, position), match_id in winners.items():
            if round_number < winners_rounds:
                self._set_double_route(
                    connection,
                    match_id,
                    winner=True,
                    destination_id=winners[
                        (round_number + 1, (position + 1) // 2)
                    ],
                    slot=1 if position % 2 else 2,
                )
            else:
                self._set_double_route(
                    connection,
                    match_id,
                    winner=True,
                    destination_id=grand_final_id,
                    slot=1,
                )

            if winners_rounds == 1:
                self._set_double_route(
                    connection,
                    match_id,
                    winner=False,
                    destination_id=grand_final_id,
                    slot=2,
                )
            elif round_number == 1:
                self._set_double_route(
                    connection,
                    match_id,
                    winner=False,
                    destination_id=losers[(1, (position + 1) // 2)],
                    slot=1 if position % 2 else 2,
                )
            else:
                losers_round = 2 * round_number - 2
                losers_count = bracket_size // (2 ** (round_number + 0))
                destination_position = losers_count - position + 1
                self._set_double_route(
                    connection,
                    match_id,
                    winner=False,
                    destination_id=losers[
                        (losers_round, destination_position)
                    ],
                    slot=2,
                )

        for (round_number, position), match_id in losers.items():
            if round_number == losers_rounds:
                destination_id = grand_final_id
                destination_slot = 2
            elif round_number % 2:
                destination_id = losers[(round_number + 1, position)]
                destination_slot = 1
            else:
                destination_id = losers[
                    (round_number + 1, (position + 1) // 2)
                ]
                destination_slot = 1 if position % 2 else 2
            self._set_double_route(
                connection,
                match_id,
                winner=True,
                destination_id=destination_id,
                slot=destination_slot,
            )

        initial_byes: list[tuple[int, int]] = []
        for position, (player1_id, player2_id) in enumerate(pairs, start=1):
            match_id = winners[(1, position)]
            if player2_id is None:
                connection.execute(
                    """
                    UPDATE matches
                    SET player1_id = ?, winner_id = ?, status = 'bye'
                    WHERE id = ?
                    """,
                    (player1_id, player1_id, match_id),
                )
                initial_byes.append((match_id, int(player1_id)))
            else:
                connection.execute(
                    """
                    UPDATE matches
                    SET player1_id = ?, player2_id = ?, status = 'ready'
                    WHERE id = ?
                    """,
                    (player1_id, player2_id, match_id),
                )

        for match_id, player_id in initial_byes:
            self._route_double_outcome(
                connection,
                match_id,
                winner_id=player_id,
                loser_id=None,
            )
        self._settle_double_bracket(connection, tournament_id)

    @staticmethod
    def _set_double_route(
        connection: sqlite3.Connection,
        match_id: int,
        *,
        winner: bool,
        destination_id: int,
        slot: int,
    ) -> None:
        match_column = "next_match_id" if winner else "loser_next_match_id"
        slot_column = "next_slot" if winner else "loser_next_slot"
        connection.execute(
            f"""
            UPDATE matches SET {match_column} = ?, {slot_column} = ?
            WHERE id = ?
            """,
            (destination_id, slot, match_id),
        )

    @staticmethod
    def _place_double_player(
        connection: sqlite3.Connection,
        destination_id: int | None,
        slot: int | None,
        player_id: int | None,
    ) -> None:
        if destination_id is None or slot is None or player_id is None:
            return
        slot_column = "player1_id" if slot == 1 else "player2_id"
        connection.execute(
            f"UPDATE matches SET {slot_column} = ? WHERE id = ?",
            (player_id, destination_id),
        )

    def _route_double_outcome(
        self,
        connection: sqlite3.Connection,
        match_id: int,
        *,
        winner_id: int | None,
        loser_id: int | None,
    ) -> None:
        route = connection.execute(
            """
            SELECT next_match_id, next_slot,
                   loser_next_match_id, loser_next_slot
            FROM matches WHERE id = ?
            """,
            (match_id,),
        ).fetchone()
        self._place_double_player(
            connection,
            route["next_match_id"],
            route["next_slot"],
            winner_id,
        )
        self._place_double_player(
            connection,
            route["loser_next_match_id"],
            route["loser_next_slot"],
            loser_id,
        )

    def _settle_double_bracket(
        self,
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> None:
        while True:
            changed = False
            pending = connection.execute(
                """
                SELECT * FROM matches
                WHERE tournament_id = ? AND status = 'pending'
                ORDER BY round_number, position
                """,
                (tournament_id,),
            ).fetchall()
            for match in pending:
                feeders = connection.execute(
                    """
                    SELECT status FROM matches
                    WHERE next_match_id = ? OR loser_next_match_id = ?
                    """,
                    (match["id"], match["id"]),
                ).fetchall()
                if not feeders or any(
                    feeder["status"] not in ("finished", "bye")
                    for feeder in feeders
                ):
                    continue
                current_match = connection.execute(
                    "SELECT * FROM matches WHERE id = ?",
                    (match["id"],),
                ).fetchone()
                player1_id = current_match["player1_id"]
                player2_id = current_match["player2_id"]
                if player1_id is not None and player2_id is not None:
                    connection.execute(
                        "UPDATE matches SET status = 'ready' WHERE id = ?",
                        (match["id"],),
                    )
                    changed = True
                    continue

                winner_id = (
                    int(player1_id)
                    if player1_id is not None
                    else int(player2_id)
                    if player2_id is not None
                    else None
                )
                normalized_player1 = winner_id
                connection.execute(
                    """
                    UPDATE matches
                    SET player1_id = ?, player2_id = NULL,
                        winner_id = ?, status = 'bye'
                    WHERE id = ?
                    """,
                    (normalized_player1, winner_id, match["id"]),
                )
                self._route_double_outcome(
                    connection,
                    int(match["id"]),
                    winner_id=winner_id,
                    loser_id=None,
                )
                if match["bracket"] == "grand_final" and winner_id is not None:
                    self._finish_tournament(
                        connection,
                        tournament_id,
                        winner_id,
                    )
                changed = True
            if not changed:
                return

    @staticmethod
    def _insert_double_group(
        connection: sqlite3.Connection,
        tournament_id: int,
        *,
        stage: int,
        position_start: int,
        player_ids: list[int],
        bracket: str,
    ) -> int:
        position = position_start
        for index in range(0, len(player_ids), 2):
            player1_id = player_ids[index]
            player2_id = (
                player_ids[index + 1]
                if index + 1 < len(player_ids)
                else None
            )
            if player2_id is None:
                connection.execute(
                    """
                    INSERT INTO matches
                        (tournament_id, round_number, position, bracket,
                         player1_id, winner_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'bye')
                    """,
                    (
                        tournament_id,
                        stage,
                        position,
                        bracket,
                        player1_id,
                        player1_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO matches
                        (tournament_id, round_number, position, bracket,
                         player1_id, player2_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'ready')
                    """,
                    (
                        tournament_id,
                        stage,
                        position,
                        bracket,
                        player1_id,
                        player2_id,
                    ),
                )
            position += 1
        return position

    def _schedule_next_double_stage(
        self,
        connection: sqlite3.Connection,
        tournament_id: int,
        stage: int,
    ) -> None:
        active_players = connection.execute(
            """
            SELECT player_id, losses FROM tournament_players
            WHERE tournament_id = ? AND losses < 2
            ORDER BY losses, seed
            """,
            (tournament_id,),
        ).fetchall()
        if len(active_players) == 1:
            self._finish_tournament(
                connection,
                tournament_id,
                int(active_players[0]["player_id"]),
            )
            return

        zero_loss = [
            int(row["player_id"]) for row in active_players if row["losses"] == 0
        ]
        one_loss = [
            int(row["player_id"]) for row in active_players if row["losses"] == 1
        ]

        if len(active_players) == 2 and len(zero_loss) == 1 and len(one_loss) == 1:
            self._insert_double_group(
                connection,
                tournament_id,
                stage=stage,
                position_start=1,
                player_ids=[zero_loss[0], one_loss[0]],
                bracket="grand_final",
            )
            return

        next_position = self._insert_double_group(
            connection,
            tournament_id,
            stage=stage,
            position_start=1,
            player_ids=zero_loss,
            bracket="winners",
        )
        self._insert_double_group(
            connection,
            tournament_id,
            stage=stage,
            position_start=next_position,
            player_ids=one_loss,
            bracket="losers",
        )

    @staticmethod
    def _start_round_robin(
        connection: sqlite3.Connection,
        tournament_id: int,
        player_ids: list[int],
    ) -> None:
        schedule = round_robin_rounds(player_ids)
        for round_number, pairs in enumerate(schedule, start=1):
            for position, (player1_id, player2_id) in enumerate(pairs, start=1):
                if player2_id is None:
                    connection.execute(
                        """
                        INSERT INTO matches
                            (tournament_id, round_number, position, bracket,
                             player1_id, winner_id, status)
                        VALUES (?, ?, ?, 'round_robin', ?, ?, 'bye')
                        """,
                        (
                            tournament_id,
                            round_number,
                            position,
                            player1_id,
                            player1_id,
                        ),
                    )
                else:
                    status = "ready" if round_number == 1 else "pending"
                    connection.execute(
                        """
                        INSERT INTO matches
                            (tournament_id, round_number, position, bracket,
                             player1_id, player2_id, status)
                        VALUES (?, ?, ?, 'round_robin', ?, ?, ?)
                        """,
                        (
                            tournament_id,
                            round_number,
                            position,
                            player1_id,
                            player2_id,
                            status,
                        ),
                    )

    def list_matches(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT m.*,
                       p1.display_name AS player1_name,
                       p2.display_name AS player2_name,
                       w.display_name AS winner_name
                FROM matches m
                LEFT JOIN players p1 ON p1.id = m.player1_id
                LEFT JOIN players p2 ON p2.id = m.player2_id
                LEFT JOIN players w ON w.id = m.winner_id
                WHERE m.tournament_id = ?
                ORDER BY m.round_number, m.position
                """,
                (tournament_id,),
            ).fetchall()

    def get_match(self, match_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT m.*,
                       p1.display_name AS player1_name,
                       p2.display_name AS player2_name,
                       w.display_name AS winner_name
                FROM matches m
                LEFT JOIN players p1 ON p1.id = m.player1_id
                LEFT JOIN players p2 ON p2.id = m.player2_id
                LEFT JOIN players w ON w.id = m.winner_id
                WHERE m.id = ?
                """,
                (match_id,),
            ).fetchone()

    def record_winner(self, match_id: int, winner_id: int) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            match = connection.execute(
                "SELECT * FROM matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            if not match:
                raise LookupError("Матч не найден.")
            if match["status"] != "ready":
                raise ValueError("Результат этого матча уже указан или матч ещё не готов.")
            if winner_id not in (match["player1_id"], match["player2_id"]):
                raise ValueError("Выбранный игрок не участвует в этом матче.")

            tournament = connection.execute(
                "SELECT * FROM tournaments WHERE id = ?",
                (match["tournament_id"],),
            ).fetchone()
            connection.execute(
                """
                UPDATE matches SET winner_id = ?, status = 'finished'
                WHERE id = ?
                """,
                (winner_id, match_id),
            )

            if tournament["format"] == "single_elimination":
                if match["next_match_id"] is not None:
                    self._place_winner_in_next_match(connection, match_id, winner_id)
                else:
                    self._finish_tournament(
                        connection,
                        int(match["tournament_id"]),
                        winner_id,
                    )
            elif tournament["format"] == "double_elimination":
                loser_id = (
                    int(match["player2_id"])
                    if winner_id == match["player1_id"]
                    else int(match["player1_id"])
                )
                connection.execute(
                    """
                    UPDATE tournament_players SET losses = losses + 1
                    WHERE tournament_id = ? AND player_id = ?
                    """,
                    (match["tournament_id"], loser_id),
                )
                if match["bracket"] in ("grand_final", "grand_final_reset"):
                    self._finish_tournament(
                        connection,
                        int(match["tournament_id"]),
                        winner_id,
                    )
                    return int(match["tournament_id"])
                if int(tournament["bracket_version"]) >= 2:
                    self._route_double_outcome(
                        connection,
                        match_id,
                        winner_id=winner_id,
                        loser_id=loser_id,
                    )
                    self._settle_double_bracket(
                        connection,
                        int(match["tournament_id"]),
                    )
                else:
                    unresolved = connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM matches
                        WHERE tournament_id = ? AND round_number = ?
                          AND status = 'ready'
                        """,
                        (match["tournament_id"], match["round_number"]),
                    ).fetchone()
                    if unresolved["count"] == 0:
                        self._schedule_next_double_stage(
                            connection,
                            int(match["tournament_id"]),
                            int(match["round_number"]) + 1,
                        )
            elif tournament["format"] == "round_robin":
                unresolved = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM matches
                    WHERE tournament_id = ? AND round_number = ?
                      AND status = 'ready'
                    """,
                    (match["tournament_id"], match["round_number"]),
                ).fetchone()
                if unresolved["count"] == 0:
                    next_round = connection.execute(
                        """
                        SELECT MIN(round_number) AS round_number FROM matches
                        WHERE tournament_id = ? AND status = 'pending'
                        """,
                        (match["tournament_id"],),
                    ).fetchone()
                    if next_round["round_number"] is not None:
                        connection.execute(
                            """
                            UPDATE matches SET status = 'ready'
                            WHERE tournament_id = ? AND round_number = ?
                              AND status = 'pending'
                            """,
                            (
                                match["tournament_id"],
                                next_round["round_number"],
                            ),
                        )
                    else:
                        champion_id = self._round_robin_champion(
                            connection,
                            int(match["tournament_id"]),
                        )
                        self._finish_tournament(
                            connection,
                            int(match["tournament_id"]),
                            champion_id,
                        )
            else:
                raise ValueError("Неизвестный формат турнира.")

            return int(match["tournament_id"])

    def change_winner(self, match_id: int, winner_id: int) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            match = connection.execute(
                "SELECT * FROM matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            if not match:
                raise LookupError("Матч не найден.")
            if match["status"] != "finished":
                raise ValueError("Изменить можно только результат завершённого матча.")
            if winner_id not in (match["player1_id"], match["player2_id"]):
                raise ValueError("Выбранный игрок не участвует в этом матче.")
            if winner_id == match["winner_id"]:
                return int(match["tournament_id"])

            tournament_id = int(match["tournament_id"])
            tournament = connection.execute(
                "SELECT * FROM tournaments WHERE id = ?",
                (tournament_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE tournaments
                SET status = 'active', champion_player_id = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (tournament_id,),
            )

            if tournament["format"] == "single_elimination":
                self._change_single_elimination_winner(
                    connection,
                    match,
                    winner_id,
                )
            elif tournament["format"] == "double_elimination":
                if int(tournament["bracket_version"]) >= 2:
                    self._change_fixed_double_winner(
                        connection,
                        match,
                        winner_id,
                    )
                else:
                    self._change_legacy_double_winner(
                        connection,
                        match,
                        winner_id,
                    )
            elif tournament["format"] == "round_robin":
                connection.execute(
                    """
                    UPDATE matches SET winner_id = ?, status = 'finished'
                    WHERE id = ?
                    """,
                    (winner_id, match_id),
                )
                unresolved = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM matches
                    WHERE tournament_id = ? AND status IN ('ready', 'pending')
                    """,
                    (tournament_id,),
                ).fetchone()
                if unresolved["count"] == 0:
                    champion_id = self._round_robin_champion(
                        connection,
                        tournament_id,
                    )
                    self._finish_tournament(
                        connection,
                        tournament_id,
                        champion_id,
                    )
            else:
                raise ValueError("Неизвестный формат турнира.")

            return tournament_id

    def _change_single_elimination_winner(
        self,
        connection: sqlite3.Connection,
        match: sqlite3.Row,
        winner_id: int,
    ) -> None:
        source = match
        while source["next_match_id"] is not None:
            next_match = connection.execute(
                "SELECT * FROM matches WHERE id = ?",
                (source["next_match_id"],),
            ).fetchone()
            if next_match["status"] == "finished":
                raise ValueError(
                    "Этот выбор уже повлиял на сыгранный следующий матч. "
                    "Его результат сохранён, поэтому исправление остановлено."
                )
            slot_column = (
                "player1_id" if source["next_slot"] == 1 else "player2_id"
            )
            connection.execute(
                f"""
                UPDATE matches
                SET {slot_column} = NULL, winner_id = NULL, status = 'pending'
                WHERE id = ?
                """,
                (next_match["id"],),
            )
            source = next_match

        connection.execute(
            """
            UPDATE matches SET winner_id = ?, status = 'finished'
            WHERE id = ?
            """,
            (winner_id, match["id"]),
        )
        if match["next_match_id"] is None:
            self._finish_tournament(
                connection,
                int(match["tournament_id"]),
                winner_id,
            )
        else:
            self._place_winner_in_next_match(
                connection,
                int(match["id"]),
                winner_id,
            )

    def _change_fixed_double_winner(
        self,
        connection: sqlite3.Connection,
        match: sqlite3.Row,
        winner_id: int,
    ) -> None:
        tournament_id = int(match["tournament_id"])
        affected_sources = {int(match["id"])}
        downstream: set[int] = set()
        queue = [int(match["id"])]
        while queue:
            source_id = queue.pop()
            route = connection.execute(
                """
                SELECT next_match_id, loser_next_match_id
                FROM matches WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
            for destination_id in (
                route["next_match_id"],
                route["loser_next_match_id"],
            ):
                if destination_id is None:
                    continue
                destination_id = int(destination_id)
                if destination_id not in downstream:
                    downstream.add(destination_id)
                    affected_sources.add(destination_id)
                    queue.append(destination_id)

        if downstream:
            placeholders = ",".join("?" for _ in downstream)
            played = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM matches
                WHERE id IN ({placeholders}) AND status = 'finished'
                """,
                tuple(downstream),
            ).fetchone()
            if played["count"]:
                raise ValueError(
                    "Этот выбор уже повлиял на сыгранный следующий матч. "
                    "Его результат сохранён, поэтому исправление остановлено."
                )

            for source_id in affected_sources:
                route = connection.execute(
                    """
                    SELECT next_match_id, next_slot,
                           loser_next_match_id, loser_next_slot
                    FROM matches WHERE id = ?
                    """,
                    (source_id,),
                ).fetchone()
                for destination_id, slot in (
                    (route["next_match_id"], route["next_slot"]),
                    (
                        route["loser_next_match_id"],
                        route["loser_next_slot"],
                    ),
                ):
                    if destination_id is None or int(destination_id) not in downstream:
                        continue
                    slot_column = "player1_id" if slot == 1 else "player2_id"
                    connection.execute(
                        f"UPDATE matches SET {slot_column} = NULL WHERE id = ?",
                        (destination_id,),
                    )
            connection.execute(
                f"""
                UPDATE matches
                SET winner_id = NULL, status = 'pending'
                WHERE id IN ({placeholders})
                """,
                tuple(downstream),
            )

        loser_id = (
            int(match["player2_id"])
            if winner_id == match["player1_id"]
            else int(match["player1_id"])
        )
        connection.execute(
            """
            UPDATE matches SET winner_id = ?, status = 'finished'
            WHERE id = ?
            """,
            (winner_id, match["id"]),
        )
        self._recalculate_double_losses(connection, tournament_id)
        if match["bracket"] == "grand_final":
            self._finish_tournament(connection, tournament_id, winner_id)
            return
        self._route_double_outcome(
            connection,
            int(match["id"]),
            winner_id=winner_id,
            loser_id=loser_id,
        )
        self._settle_double_bracket(connection, tournament_id)

    @staticmethod
    def _recalculate_double_losses(
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> None:
        connection.execute(
            """
            UPDATE tournament_players SET losses = 0
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        )
        completed = connection.execute(
            """
            SELECT player1_id, player2_id, winner_id
            FROM matches
            WHERE tournament_id = ? AND status = 'finished'
              AND player1_id IS NOT NULL AND player2_id IS NOT NULL
            """,
            (tournament_id,),
        ).fetchall()
        for completed_match in completed:
            loser_id = (
                int(completed_match["player2_id"])
                if completed_match["winner_id"] == completed_match["player1_id"]
                else int(completed_match["player1_id"])
            )
            connection.execute(
                """
                UPDATE tournament_players SET losses = losses + 1
                WHERE tournament_id = ? AND player_id = ?
                """,
                (tournament_id, loser_id),
            )

    def _change_legacy_double_winner(
        self,
        connection: sqlite3.Connection,
        match: sqlite3.Row,
        winner_id: int,
    ) -> None:
        tournament_id = int(match["tournament_id"])
        stage = int(match["round_number"])
        later_results = connection.execute(
            """
            SELECT COUNT(*) AS count FROM matches
            WHERE tournament_id = ? AND round_number > ? AND status = 'finished'
            """,
            (tournament_id, stage),
        ).fetchone()
        if later_results["count"]:
            raise ValueError(
                "Этот выбор уже повлиял на сыгранный следующий матч. "
                "Его результат сохранён, поэтому исправление остановлено."
            )
        connection.execute(
            """
            DELETE FROM matches
            WHERE tournament_id = ? AND round_number > ?
            """,
            (tournament_id, stage),
        )
        connection.execute(
            """
            UPDATE matches SET winner_id = ?, status = 'finished'
            WHERE id = ?
            """,
            (winner_id, match["id"]),
        )
        self._recalculate_double_losses(connection, tournament_id)

        if match["bracket"] in ("grand_final", "grand_final_reset"):
            self._finish_tournament(connection, tournament_id, winner_id)
            return

        unresolved = connection.execute(
            """
            SELECT COUNT(*) AS count FROM matches
            WHERE tournament_id = ? AND round_number = ? AND status = 'ready'
            """,
            (tournament_id, stage),
        ).fetchone()
        if unresolved["count"] == 0:
            self._schedule_next_double_stage(
                connection,
                tournament_id,
                stage + 1,
            )

    def get_standings(self, tournament_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = self._get_standings(connection, tournament_id)
            return [
                {
                    "rank": index,
                    "player_id": int(row["player_id"]),
                    "display_name": row["display_name"],
                    "wins": int(row["wins"]),
                    "losses": int(row["match_losses"]),
                    "played": int(row["played"]),
                }
                for index, row in enumerate(rows, start=1)
            ]

    @staticmethod
    def _get_standings(
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT tp.player_id, tp.seed, p.display_name,
                   SUM(CASE
                       WHEN m.status = 'finished' AND m.winner_id = tp.player_id
                       THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE
                       WHEN m.status = 'finished'
                        AND m.winner_id != tp.player_id
                       THEN 1 ELSE 0 END) AS match_losses,
                   SUM(CASE WHEN m.status = 'finished' THEN 1 ELSE 0 END) AS played
            FROM tournament_players tp
            JOIN players p ON p.id = tp.player_id
            LEFT JOIN matches m
              ON m.tournament_id = tp.tournament_id
             AND m.bracket = 'round_robin'
             AND (m.player1_id = tp.player_id OR m.player2_id = tp.player_id)
            WHERE tp.tournament_id = ?
            GROUP BY tp.player_id, tp.seed, p.display_name
            ORDER BY wins DESC, match_losses ASC, tp.seed ASC
            """,
            (tournament_id,),
        ).fetchall()

    def _round_robin_champion(
        self,
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> int:
        standings = self._get_standings(connection, tournament_id)
        if not standings:
            raise ValueError("Не удалось определить победителя.")
        top_wins = standings[0]["wins"]
        leaders = [row for row in standings if row["wins"] == top_wins]
        if len(leaders) == 2:
            first_id = int(leaders[0]["player_id"])
            second_id = int(leaders[1]["player_id"])
            head_to_head = connection.execute(
                """
                SELECT winner_id FROM matches
                WHERE tournament_id = ? AND bracket = 'round_robin'
                  AND status = 'finished'
                  AND (
                    (player1_id = ? AND player2_id = ?)
                    OR (player1_id = ? AND player2_id = ?)
                  )
                """,
                (
                    tournament_id,
                    first_id,
                    second_id,
                    second_id,
                    first_id,
                ),
            ).fetchone()
            if head_to_head:
                return int(head_to_head["winner_id"])
        return int(leaders[0]["player_id"])

    @staticmethod
    def _finish_tournament(
        connection: sqlite3.Connection,
        tournament_id: int,
        champion_id: int,
    ) -> None:
        connection.execute(
            """
            UPDATE tournaments
            SET status = 'finished', champion_player_id = ?, finished_at = ?
            WHERE id = ?
            """,
            (champion_id, _utc_now(), tournament_id),
        )
        Database._recalculate_all_ratings(connection)

    @staticmethod
    def _recalculate_all_ratings(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE players
            SET rating = CASE
                    WHEN telegram_id IS NOT NULL AND is_test = 0 THEN ?
                    ELSE NULL
                END,
                rated_games = 0
            """,
            (INITIAL_RATING,),
        )
        connection.execute("DELETE FROM rating_history")
        tournaments = connection.execute(
            """
            SELECT id FROM tournaments
            WHERE status = 'finished'
            ORDER BY COALESCE(finished_at, created_at), id
            """
        ).fetchall()
        for tournament in tournaments:
            Database._apply_tournament_ratings(
                connection,
                int(tournament["id"]),
            )

    @staticmethod
    def _apply_tournament_ratings(
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> None:
        rated_players = connection.execute(
            """
            SELECT p.id, p.rating, p.rated_games
            FROM tournament_players tp
            JOIN players p ON p.id = tp.player_id
            WHERE tp.tournament_id = ?
              AND p.telegram_id IS NOT NULL
              AND p.is_test = 0
              AND p.rating IS NOT NULL
            """,
            (tournament_id,),
        ).fetchall()
        snapshots = {
            int(player["id"]): {
                "rating": int(player["rating"]),
                "rated_games": int(player["rated_games"]),
            }
            for player in rated_players
        }
        if not snapshots:
            return

        score_differences = {player_id: 0.0 for player_id in snapshots}
        games = {player_id: 0 for player_id in snapshots}
        matches = connection.execute(
            """
            SELECT player1_id, player2_id, winner_id
            FROM matches
            WHERE tournament_id = ? AND status = 'finished'
              AND player1_id IS NOT NULL AND player2_id IS NOT NULL
            ORDER BY round_number, position
            """,
            (tournament_id,),
        ).fetchall()
        for match in matches:
            player1_id = int(match["player1_id"])
            player2_id = int(match["player2_id"])
            if player1_id not in snapshots or player2_id not in snapshots:
                continue
            player1_rating = snapshots[player1_id]["rating"]
            player2_rating = snapshots[player2_id]["rating"]
            difference = max(-400, min(400, player2_rating - player1_rating))
            expected1 = 1.0 / (1.0 + 10.0 ** (difference / 400.0))
            expected2 = 1.0 - expected1
            actual1 = 1.0 if match["winner_id"] == player1_id else 0.0
            actual2 = 1.0 - actual1
            score_differences[player1_id] += actual1 - expected1
            score_differences[player2_id] += actual2 - expected2
            games[player1_id] += 1
            games[player2_id] += 1

        calculated_at = _utc_now()
        for player_id, snapshot in snapshots.items():
            games_count = games[player_id]
            k_factor = (
                PROVISIONAL_K
                if snapshot["rated_games"] < PROVISIONAL_GAMES
                else ESTABLISHED_K
            )
            if games_count and k_factor * games_count > 700:
                k_factor = max(1, 700 // games_count)
            raw_delta = k_factor * score_differences[player_id]
            delta = (
                math.floor(raw_delta + 0.5)
                if raw_delta >= 0
                else math.ceil(raw_delta - 0.5)
            )
            rating_before = snapshot["rating"]
            rating_after = max(100, rating_before + delta)
            delta = rating_after - rating_before
            connection.execute(
                """
                UPDATE players
                SET rating = ?, rated_games = rated_games + ?
                WHERE id = ?
                """,
                (rating_after, games_count, player_id),
            )
            connection.execute(
                """
                INSERT INTO rating_history
                    (tournament_id, player_id, rating_before, rating_after,
                     delta, games, calculated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament_id,
                    player_id,
                    rating_before,
                    rating_after,
                    delta,
                    games_count,
                    calculated_at,
                ),
            )

    @staticmethod
    def _participant_count(
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count FROM tournament_players
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _ensure_registration_open(
        connection: sqlite3.Connection,
        tournament_id: int,
    ) -> sqlite3.Row:
        tournament = connection.execute(
            "SELECT * FROM tournaments WHERE id = ?",
            (tournament_id,),
        ).fetchone()
        if not tournament:
            raise LookupError("Турнир не найден.")
        if tournament["status"] != "registration":
            raise ValueError("Состав уже нельзя менять после старта турнира.")
        return tournament

    @staticmethod
    def _place_winner_in_next_match(
        connection: sqlite3.Connection,
        source_match_id: int,
        winner_id: int,
    ) -> None:
        source = connection.execute(
            """
            SELECT next_match_id, next_slot FROM matches WHERE id = ?
            """,
            (source_match_id,),
        ).fetchone()
        if source["next_match_id"] is None:
            return

        slot_column = "player1_id" if source["next_slot"] == 1 else "player2_id"
        connection.execute(
            f"UPDATE matches SET {slot_column} = ? WHERE id = ?",
            (winner_id, source["next_match_id"]),
        )
        next_match = connection.execute(
            "SELECT player1_id, player2_id FROM matches WHERE id = ?",
            (source["next_match_id"],),
        ).fetchone()
        if next_match["player1_id"] is not None and next_match["player2_id"] is not None:
            connection.execute(
                "UPDATE matches SET status = 'ready' WHERE id = ?",
                (source["next_match_id"],),
            )
