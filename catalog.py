from __future__ import annotations

import importlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional


class Catalog:
    """Catalog persistence with SQLite fallback and PostgreSQL support.

    SQLite remains the default for local development. Set DATABASE_URL to a
    PostgreSQL connection string in production (for example on Render).
    """

    def __init__(self, database_path: str = "catalog.db", database_url: str = "") -> None:
        self.database_path = Path(database_path)
        self.database_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
        self.is_postgres = bool(self.database_url)
        if not self.is_postgres:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        if self.is_postgres:
            try:
                psycopg_module = importlib.import_module("psycopg")
                dict_row = importlib.import_module("psycopg.rows").dict_row
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed") from exc
            connection: Any = psycopg_module.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=15,
            )
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return

        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _execute(self, connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.is_postgres:
            sql = sql.replace("?", "%s")
        return connection.execute(sql, params)

    def _initialize(self) -> None:
        with self._connect() as connection:
            if self.is_postgres:
                for statement in (
                    """
                    CREATE TABLE IF NOT EXISTS items (
                        id BIGSERIAL PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('movie', 'series')),
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        release_year INTEGER,
                        poster_url TEXT NOT NULL DEFAULT '',
                        stream_url TEXT NOT NULL DEFAULT '',
                        backdrop_url TEXT NOT NULL DEFAULT '',
                        quality TEXT NOT NULL DEFAULT '',
                        genre TEXT NOT NULL DEFAULT '',
                        rating DOUBLE PRECISION,
                        tmdb_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS seasons (
                        id BIGSERIAL PRIMARY KEY,
                        series_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                        season_number INTEGER NOT NULL,
                        UNIQUE(series_id, season_number)
                    );
                    CREATE TABLE IF NOT EXISTS episodes (
                        id BIGSERIAL PRIMARY KEY,
                        season_id BIGINT NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
                        episode_number INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        stream_url TEXT NOT NULL DEFAULT '',
                        quality TEXT NOT NULL DEFAULT '',
                        UNIQUE(season_id, episode_number)
                    );
                    CREATE TABLE IF NOT EXISTS uploads (
                        id BIGSERIAL PRIMARY KEY,
                        original_name TEXT NOT NULL,
                        file_size BIGINT NOT NULL DEFAULT 0,
                        mime_type TEXT NOT NULL DEFAULT '',
                        stream_url TEXT NOT NULL,
                        chat_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL,
                        catalog_item_id BIGINT,
                        created_at TEXT NOT NULL,
                        UNIQUE(chat_id, message_id)
                    );
                    CREATE TABLE IF NOT EXISTS bot_admins (
                        user_id BIGINT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bot_users (
                        user_id BIGINT PRIMARY KEY,
                        approved_by BIGINT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS access_notices (
                        scope_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bot_chats (
                        chat_id BIGINT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        chat_type TEXT NOT NULL,
                        registered_by BIGINT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                ).split(";"):
                    if statement.strip():
                        connection.execute(statement)
            else:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL CHECK(kind IN ('movie', 'series')),
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        release_year INTEGER,
                        poster_url TEXT NOT NULL DEFAULT '',
                        stream_url TEXT NOT NULL DEFAULT '',
                        backdrop_url TEXT NOT NULL DEFAULT '',
                        quality TEXT NOT NULL DEFAULT '',
                        genre TEXT NOT NULL DEFAULT '',
                        rating REAL,
                        tmdb_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS seasons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        series_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                        season_number INTEGER NOT NULL,
                        UNIQUE(series_id, season_number)
                    );
                    CREATE TABLE IF NOT EXISTS episodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
                        episode_number INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        stream_url TEXT NOT NULL DEFAULT '',
                        quality TEXT NOT NULL DEFAULT '',
                        UNIQUE(season_id, episode_number)
                    );
                    CREATE TABLE IF NOT EXISTS uploads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        original_name TEXT NOT NULL,
                        file_size INTEGER NOT NULL DEFAULT 0,
                        mime_type TEXT NOT NULL DEFAULT '',
                        stream_url TEXT NOT NULL,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        catalog_item_id INTEGER,
                        created_at TEXT NOT NULL,
                        UNIQUE(chat_id, message_id)
                    );
                    CREATE TABLE IF NOT EXISTS bot_admins (user_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS bot_users (user_id INTEGER PRIMARY KEY, approved_by INTEGER NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS access_notices (scope_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS bot_chats (
                        chat_id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        chat_type TEXT NOT NULL,
                        registered_by INTEGER NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )

            for statement in (
                "CREATE INDEX IF NOT EXISTS idx_items_kind_title ON items(kind, title)",
                "CREATE INDEX IF NOT EXISTS idx_items_updated_at ON items(updated_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_seasons_series_number ON seasons(series_id, season_number)",
                "CREATE INDEX IF NOT EXISTS idx_episodes_season_number ON episodes(season_id, episode_number)",
                "CREATE INDEX IF NOT EXISTS idx_uploads_created_at ON uploads(created_at DESC)",
            ):
                connection.execute(statement)

            if not self.is_postgres:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(items)").fetchall()}
                migrations = {
                    "backdrop_url": "ALTER TABLE items ADD COLUMN backdrop_url TEXT NOT NULL DEFAULT ''",
                    "rating": "ALTER TABLE items ADD COLUMN rating REAL",
                    "tmdb_id": "ALTER TABLE items ADD COLUMN tmdb_id INTEGER",
                    "quality": "ALTER TABLE items ADD COLUMN quality TEXT NOT NULL DEFAULT ''",
                    "genre": "ALTER TABLE items ADD COLUMN genre TEXT NOT NULL DEFAULT ''",
                }
                for column, statement in migrations.items():
                    if column not in columns:
                        connection.execute(statement)
                episode_columns = {row[1] for row in connection.execute("PRAGMA table_info(episodes)").fetchall()}
                if "quality" not in episode_columns:
                    connection.execute("ALTER TABLE episodes ADD COLUMN quality TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def add_item(self, kind: str, title: str, summary: str = "", release_year: Optional[int] = None,
                 poster_url: str = "", stream_url: str = "", backdrop_url: str = "",
                 rating: Optional[float] = None, tmdb_id: Optional[int] = None,
                 quality: str = "", genre: str = "") -> int:
        now = self._now()
        with self._connect() as connection:
            row = self._execute(connection, """INSERT INTO items
                (kind, title, summary, release_year, poster_url, stream_url, backdrop_url,
                 rating, tmdb_id, quality, genre, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (kind, title, summary, release_year, poster_url, stream_url, backdrop_url,
                 rating, tmdb_id, quality, genre, now, now)).fetchone()
            if row is None:
                raise RuntimeError("Could not create catalog item")
            return int(row["id"] if self.is_postgres else row[0])

    def update_item(self, item_id: int, **fields: Any) -> None:
        allowed = {"title", "summary", "release_year", "poster_url", "stream_url", "backdrop_url", "rating", "tmdb_id", "quality", "genre"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            self._execute(connection, f"UPDATE items SET {assignments} WHERE id = ?", (*updates.values(), item_id))

    def get_item(self, item_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = self._execute(connection, "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def search(self, query: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
        pattern = f"%{query.strip()}%"
        sql = "SELECT * FROM items WHERE (title LIKE ? OR summary LIKE ?)"
        parameters: list[Any] = [pattern, pattern]
        if kind:
            sql += " AND kind = ?"
            parameters.append(kind)
        sql += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            return [dict(row) for row in self._execute(connection, sql, tuple(parameters)).fetchall()]

    def list_items(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        return self.search("", kind)

    def delete_item(self, item_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "DELETE FROM items WHERE id = ?", (item_id,))

    def add_season(self, series_id: int, season_number: int) -> int:
        with self._connect() as connection:
            self._execute(connection, "INSERT INTO seasons (series_id, season_number) VALUES (?, ?) ON CONFLICT(series_id, season_number) DO NOTHING", (series_id, season_number))
            row = self._execute(connection, "SELECT id FROM seasons WHERE series_id = ? AND season_number = ?", (series_id, season_number)).fetchone()
            if row is None:
                raise RuntimeError("Could not create season")
            return int(row["id"] if self.is_postgres else row[0])

    def list_seasons(self, series_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in self._execute(connection, "SELECT * FROM seasons WHERE series_id = ? ORDER BY season_number", (series_id,)).fetchall()]

    def delete_season(self, series_id: int, season_number: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "DELETE FROM seasons WHERE series_id = ? AND season_number = ?", (series_id, season_number))

    def add_episode(self, series_id: int, season_number: int, episode_number: int, title: str,
                    stream_url: str = "", quality: str = "") -> int:
        season_id = self.add_season(series_id, season_number)
        with self._connect() as connection:
            self._execute(connection, """INSERT INTO episodes (season_id, episode_number, title, stream_url, quality)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(season_id, episode_number) DO UPDATE SET
                title = excluded.title, stream_url = excluded.stream_url, quality = excluded.quality""",
                (season_id, episode_number, title, stream_url, quality))
            row = self._execute(connection, "SELECT id FROM episodes WHERE season_id = ? AND episode_number = ?", (season_id, episode_number)).fetchone()
            if row is None:
                raise RuntimeError("Could not create episode")
            return int(row["id"] if self.is_postgres else row[0])

    def list_episodes(self, series_id: int, season_number: Optional[int] = None) -> list[dict[str, Any]]:
        sql = """SELECT episodes.*, seasons.season_number FROM episodes
                 JOIN seasons ON seasons.id = episodes.season_id WHERE seasons.series_id = ?"""
        parameters: list[Any] = [series_id]
        if season_number is not None:
            sql += " AND seasons.season_number = ?"
            parameters.append(season_number)
        sql += " ORDER BY seasons.season_number, episodes.episode_number"
        with self._connect() as connection:
            return [dict(row) for row in self._execute(connection, sql, tuple(parameters)).fetchall()]

    def delete_episode(self, series_id: int, season_number: int, episode_number: int) -> None:
        with self._connect() as connection:
            self._execute(connection, """DELETE FROM episodes WHERE season_id IN
                (SELECT id FROM seasons WHERE series_id = ? AND season_number = ?) AND episode_number = ?""",
                (series_id, season_number, episode_number))

    def save_upload(self, original_name: str, file_size: int, mime_type: str, stream_url: str,
                    chat_id: int, message_id: int, catalog_item_id: Optional[int] = None) -> int:
        with self._connect() as connection:
            existing = self._execute(connection, "SELECT id FROM uploads WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
            if existing:
                return int(existing["id"] if self.is_postgres else existing[0])
            if self.is_postgres:
                row = self._execute(connection, """INSERT INTO uploads
                    (original_name, file_size, mime_type, stream_url, chat_id, message_id, catalog_item_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                    (original_name, file_size, mime_type, stream_url, chat_id, message_id, catalog_item_id, self._now())).fetchone()
                return int(row["id"])
            cursor = self._execute(connection, """INSERT INTO uploads
                (original_name, file_size, mime_type, stream_url, chat_id, message_id, catalog_item_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (original_name, file_size, mime_type, stream_url, chat_id, message_id, catalog_item_id, self._now()))
            if cursor.lastrowid is None:
                raise RuntimeError("Could not save upload")
            return int(cursor.lastrowid)

    def attach_upload(self, upload_id: int, catalog_item_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "UPDATE uploads SET catalog_item_id = ? WHERE id = ?", (catalog_item_id, upload_id))

    def update_upload_stream_url(self, upload_id: int, stream_url: str) -> None:
        with self._connect() as connection:
            self._execute(connection, "UPDATE uploads SET stream_url = ? WHERE id = ?", (stream_url, upload_id))
            upload = self._execute(connection, "SELECT catalog_item_id FROM uploads WHERE id = ?", (upload_id,)).fetchone()
            if upload is None:
                return
            catalog_item_value = upload["catalog_item_id"] if self.is_postgres else upload[0]
            if catalog_item_value is None:
                return
            catalog_item_id = int(catalog_item_value)

            item_row = self._execute(connection, "SELECT id FROM items WHERE id = ?", (catalog_item_id,)).fetchone()
            if item_row is not None:
                self._execute(connection, "UPDATE items SET stream_url = ? WHERE id = ?", (stream_url, catalog_item_id))

            episode_row = self._execute(connection, "SELECT id FROM episodes WHERE id = ?", (catalog_item_id,)).fetchone()
            if episode_row is not None:
                self._execute(connection, "UPDATE episodes SET stream_url = ? WHERE id = ?", (stream_url, catalog_item_id))

    def list_uploads(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in self._execute(connection, "SELECT * FROM uploads ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            def count(sql: str) -> int:
                row = self._execute(connection, sql).fetchone()
                return int(row["count"] if self.is_postgres else row[0])
            return {"movies": count("SELECT COUNT(*) FROM items WHERE kind = 'movie'"), "series": count("SELECT COUNT(*) FROM items WHERE kind = 'series'"), "seasons": count("SELECT COUNT(*) FROM seasons"), "episodes": count("SELECT COUNT(*) FROM episodes"), "uploads": count("SELECT COUNT(*) FROM uploads")}

    def checkpoint(self) -> None:
        if self.is_postgres:
            return
        with self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def add_admin(self, user_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "INSERT INTO bot_admins (user_id, created_at) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING", (user_id, self._now()))

    def remove_admin(self, user_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "DELETE FROM bot_admins WHERE user_id = ?", (user_id,))

    def list_admins(self) -> list[int]:
        with self._connect() as connection:
            return [int(row["user_id"]) for row in self._execute(connection, "SELECT user_id FROM bot_admins ORDER BY user_id").fetchall()]

    def add_user(self, user_id: int, approved_by: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "INSERT INTO bot_users (user_id, approved_by, created_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET approved_by = excluded.approved_by", (user_id, approved_by, self._now()))

    def remove_user(self, user_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "DELETE FROM bot_users WHERE user_id = ?", (user_id,))

    def list_users(self) -> list[int]:
        with self._connect() as connection:
            return [int(row["user_id"]) for row in self._execute(connection, "SELECT user_id FROM bot_users ORDER BY user_id").fetchall()]

    def is_user_approved(self, user_id: int) -> bool:
        with self._connect() as connection:
            return self._execute(connection, "SELECT 1 FROM bot_users WHERE user_id = ?", (user_id,)).fetchone() is not None

    def claim_access_notice(self, scope_id: str) -> bool:
        with self._connect() as connection:
            cursor = self._execute(connection, "INSERT INTO access_notices (scope_id, created_at) VALUES (?, ?) ON CONFLICT(scope_id) DO NOTHING", (scope_id, self._now()))
        return cursor.rowcount == 1

    def register_chat(self, chat_id: int, title: str, chat_type: str, registered_by: int) -> None:
        now = self._now()
        with self._connect() as connection:
            self._execute(connection, """INSERT INTO bot_chats (chat_id, title, chat_type, registered_by, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title,
                chat_type = excluded.chat_type, registered_by = excluded.registered_by, active = 1, updated_at = excluded.updated_at""",
                (chat_id, title, chat_type, registered_by, now, now))

    def unregister_chat(self, chat_id: int) -> None:
        with self._connect() as connection:
            self._execute(connection, "DELETE FROM bot_chats WHERE chat_id = ?", (chat_id,))

    def list_bot_chats(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in self._execute(connection, "SELECT * FROM bot_chats WHERE active = 1 ORDER BY title, chat_id").fetchall()]

    def is_chat_registered(self, chat_id: int) -> bool:
        with self._connect() as connection:
            return self._execute(connection, "SELECT 1 FROM bot_chats WHERE chat_id = ? AND active = 1", (chat_id,)).fetchone() is not None

    def integrity_report(self) -> dict[str, Any]:
        with self._connect() as connection:
            duplicate_rows: list[dict[str, Any]] = self._execute(connection, """SELECT kind, lower(trim(title)) AS normalized_title,
                MIN(title) AS title, STRING_AGG(CAST(id AS TEXT), ',') AS item_ids, COUNT(*) AS count
                FROM items GROUP BY kind, normalized_title HAVING COUNT(*) > 1 ORDER BY kind, normalized_title""").fetchall() if self.is_postgres else self._execute(connection, """SELECT kind, lower(trim(title)) AS normalized_title,
                MIN(title) AS title, GROUP_CONCAT(id) AS item_ids, COUNT(*) AS count FROM items
                GROUP BY kind, normalized_title HAVING COUNT(*) > 1 ORDER BY kind, normalized_title""").fetchall()
            series_rows: list[dict[str, Any]] = self._execute(connection, """SELECT items.id AS series_id, items.title AS series_title,
                seasons.season_number, STRING_AGG(CAST(episodes.episode_number AS TEXT), ',') AS episode_numbers
                FROM items JOIN seasons ON seasons.series_id = items.id LEFT JOIN episodes ON episodes.season_id = seasons.id
                WHERE items.kind = 'series' GROUP BY items.id, items.title, seasons.season_number ORDER BY items.title, seasons.season_number""").fetchall() if self.is_postgres else self._execute(connection, """SELECT items.id AS series_id, items.title AS series_title,
                seasons.season_number, GROUP_CONCAT(episodes.episode_number) AS episode_numbers FROM items
                JOIN seasons ON seasons.series_id = items.id LEFT JOIN episodes ON episodes.season_id = seasons.id
                WHERE items.kind = 'series' GROUP BY items.id, items.title, seasons.season_number ORDER BY items.title, seasons.season_number""").fetchall()
            item_rows = self._execute(connection, "SELECT id, title, kind, poster_url, summary FROM items ORDER BY kind, title").fetchall()

        duplicates: list[dict[str, Any]] = [{"kind": row["kind"], "title": row["title"], "item_ids": sorted(int(value) for value in row["item_ids"].split(",")), "count": int(row["count"])} for row in duplicate_rows]
        missing_episodes: list[dict[str, Any]] = []
        for row in series_rows:
            if not row["episode_numbers"]:
                continue
            numbers = sorted({int(value) for value in row["episode_numbers"].split(",")})
            if len(numbers) < 2:
                continue
            missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
            if missing:
                missing_episodes.append({"series_id": int(row["series_id"]), "series_title": row["series_title"], "season_number": int(row["season_number"]), "missing_episodes": missing})

        missing_posters: list[dict[str, Any]] = []
        empty_summaries: list[dict[str, Any]] = []
        for row in item_rows:
            item_id = int(row["id"] if self.is_postgres else row[0])
            title = str(row["title"] if self.is_postgres else row[1])
            poster_url = str(row["poster_url"] if self.is_postgres else row[3])
            summary = str(row["summary"] if self.is_postgres else row[4])
            if not poster_url.strip():
                missing_posters.append({"item_id": item_id, "title": title, "kind": row["kind"] if self.is_postgres else row[2]})
            if not summary.strip():
                empty_summaries.append({"item_id": item_id, "title": title, "kind": row["kind"] if self.is_postgres else row[2]})

        return {
            "duplicates": duplicates,
            "missing_episodes": missing_episodes,
            "missing_posters": missing_posters,
            "empty_summaries": empty_summaries,
        }

    def close(self) -> None:
        """Compatibility hook; connections are short-lived and closed per operation."""
        return None
