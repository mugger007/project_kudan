from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class SqliteStore:
    def __init__(self, path: str):
        self.path = self._resolve_db_path(path)

    @staticmethod
    def _resolve_db_path(path: str) -> str:
        """Resolves DB path so one config works in local Windows and Linux containers."""
        raw = (path or "./kudan.db").strip()

        # Keep container-native path untouched on Linux/macOS runtimes.
        if os.name != "nt":
            return raw

        # On Windows local runs, '/data/...' is not a valid writable root path.
        if raw.startswith("/data/"):
            relative = raw.removeprefix("/data/")
            return str((Path("./data") / relative).resolve())

        return str(Path(raw).resolve())

    def _ensure_parent_dir(self) -> None:
        """Creates parent directories for the DB file if they do not exist."""
        db_file = Path(self.path)
        parent = db_file.parent
        if str(parent) and str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> aiosqlite.Connection:
        """Returns a fresh SQLite connection context after ensuring filesystem prerequisites."""
        self._ensure_parent_dir()
        return aiosqlite.connect(self.path)

    async def init(self) -> None:
        async with self._connect() as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    edge REAL NOT NULL,
                    confidence REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    status TEXT NOT NULL,
                    tx_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    updated_ts TEXT NOT NULL,
                    PRIMARY KEY (market_id, side)
                );

                CREATE TABLE IF NOT EXISTS candidate_events (
                    event_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    endDate TEXT,
                    tweetCount INTEGER,
                    event_type TEXT NOT NULL DEFAULT 'tweet',
                    current_price REAL,
                    bucket TEXT NOT NULL,
                    raw_data TEXT NOT NULL,
                    last_fetched TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS filtered_events (
                    event_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    last_fetched TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_candidate_events_bucket
                ON candidate_events (bucket);

                CREATE INDEX IF NOT EXISTS idx_filtered_events_classification
                ON filtered_events (classification);
                """
            )

            cursor = await db.execute("PRAGMA table_info(candidate_events)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "event_type" not in columns:
                await db.execute("ALTER TABLE candidate_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'tweet'")
            if "current_price" not in columns:
                await db.execute("ALTER TABLE candidate_events ADD COLUMN current_price REAL")

            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candidate_events_type_bucket
                ON candidate_events (event_type, bucket)
                """
            )

            await db.commit()

    async def log_scan(self, market_id: str, strategy: str, payload: dict) -> None:
        await self._insert("scan_log", market_id, strategy, payload)

    async def log_opportunity(
        self,
        market_id: str,
        strategy: str,
        side: str,
        edge: float,
        confidence: float,
        metadata: dict,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO opportunities (ts, market_id, strategy, side, edge, confidence, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    market_id,
                    strategy,
                    side,
                    edge,
                    confidence,
                    json.dumps(metadata),
                ),
            )
            await db.commit()

    async def log_trade(
        self,
        market_id: str,
        strategy: str,
        side: str,
        price: float,
        size: float,
        status: str,
        tx_hash: str = "",
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO trades (ts, market_id, strategy, side, price, size, status, tx_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    market_id,
                    strategy,
                    side,
                    price,
                    size,
                    status,
                    tx_hash,
                ),
            )
            await db.commit()

    async def _insert(self, table: str, market_id: str, strategy: str, payload: dict) -> None:
        async with self._connect() as db:
            await db.execute(
                f"INSERT INTO {table} (ts, market_id, strategy, payload_json) VALUES (?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    market_id,
                    strategy,
                    json.dumps(payload),
                ),
            )
            await db.commit()

    async def replace_candidate_events(self, rows: list[dict]) -> None:
        """Replaces candidate_events table contents with current shortlisted snapshot."""
        fetched_ts = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.execute("DELETE FROM candidate_events")
            for row in rows:
                await db.execute(
                    """
                    INSERT INTO candidate_events (event_id, title, endDate, tweetCount, event_type, current_price, bucket, raw_data, last_fetched)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("event_id") or ""),
                        str(row.get("title") or ""),
                        str(row.get("endDate") or ""),
                        row.get("tweetCount"),
                        str(row.get("event_type") or "tweet"),
                        row.get("current_price"),
                        str(row.get("bucket") or "monthly"),
                        json.dumps(row.get("raw_data") or {}),
                        fetched_ts,
                    ),
                )
            await db.commit()

    async def replace_filtered_events(self, rows: list[dict]) -> None:
        """Replaces filtered_events with pre-matcher classified event snapshot."""
        fetched_ts = datetime.now(timezone.utc).isoformat()
        async with self._connect() as db:
            await db.execute("DELETE FROM filtered_events")
            for row in rows:
                await db.execute(
                    """
                    INSERT INTO filtered_events (event_id, title, classification, last_fetched)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(row.get("event_id") or ""),
                        str(row.get("title") or ""),
                        str(row.get("classification") or ""),
                        fetched_ts,
                    ),
                )
            await db.commit()

    async def list_candidate_events(self, bucket: str) -> list[dict]:
        """Returns cached candidate events for the given bucket."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT event_id, title, endDate, tweetCount, event_type, current_price, bucket, raw_data, last_fetched
                FROM candidate_events
                WHERE bucket = ?
                ORDER BY endDate ASC
                """,
                (bucket,),
            )
            rows = await cursor.fetchall()

        events: list[dict] = []
        for row in rows:
            events.append(
                {
                    "event_id": row[0],
                    "title": row[1],
                    "endDate": row[2],
                    "tweetCount": row[3],
                    "event_type": row[4],
                    "current_price": row[5],
                    "bucket": row[6],
                    "raw_data": json.loads(row[7] or "{}"),
                    "last_fetched": row[8],
                }
            )
        return events
