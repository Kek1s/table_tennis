from __future__ import annotations

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


SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    display_name TEXT NOT NULL,
    username TEXT,
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
    UNIQUE (tournament_id, round_number, position)
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

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
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

    def register_telegram_player(
        self,
        telegram_id: int,
        display_name: str,
        username: str | None,
    ) -> sqlite3.Row:
        display_name = _clean_name(display_name)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO players (telegram_id, display_name, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username
                """,
                (telegram_id, display_name, username, _utc_now()),
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

    def list_tournament_players(self, tournament_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT p.*, tp.seed, tp.losses
                FROM tournament_players tp
                JOIN players p ON p.id = tp.player_id
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
                WHERE p.telegram_id IS NOT NULL
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
                WHERE p.telegram_id IS NOT NULL
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
                SET status = 'active', started_at = ?
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
        shuffled = list(player_ids)
        (rng or random.SystemRandom()).shuffle(shuffled)
        self._insert_double_group(
            connection,
            tournament_id,
            stage=1,
            position_start=1,
            player_ids=shuffled,
            bracket="winners",
        )

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
        if len(active_players) == 2 and not zero_loss:
            self._insert_double_group(
                connection,
                tournament_id,
                stage=stage,
                position_start=1,
                player_ids=one_loss,
                bracket="grand_final_reset",
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
