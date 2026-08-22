from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from poly_weather.domain import Market

_SAFE_PART = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class RawEventArchive:
    """Append-only JSONL archive partitioned by source and UTC date."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def append(
        self,
        *,
        source: str,
        fetched_at: datetime,
        request_url: str,
        payload: Any,
    ) -> Path:
        if not _SAFE_PART.fullmatch(source):
            raise ValueError(f"unsafe archive source name: {source!r}")
        utc_time = fetched_at.astimezone(UTC)
        target = self.root / source / utc_time.date().isoformat() / "events.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "event_id": str(uuid4()),
            "source": source,
            "fetched_at": utc_time.isoformat(),
            "request_url": request_url,
            "payload": payload,
        }
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        return target


class CatalogStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> CatalogStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                version INTEGER NOT NULL
            );
            INSERT INTO schema_meta(version)
            SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                run_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                request_url TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                raw_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                slug TEXT NOT NULL,
                condition_id TEXT,
                category TEXT,
                active INTEGER NOT NULL,
                closed INTEGER NOT NULL,
                end_date TEXT,
                resolution_source TEXT,
                outcomes_json TEXT NOT NULL,
                prices_json TEXT NOT NULL,
                clob_token_ids_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS markets_slug_idx ON markets(slug);
            CREATE INDEX IF NOT EXISTS markets_active_idx ON markets(active, closed);
            """
        )
        self.connection.commit()

    def record_page(
        self,
        *,
        source: str,
        fetched_at: datetime,
        request_url: str,
        item_count: int,
        candidate_count: int,
        raw_path: Path,
    ) -> str:
        run_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO ingestion_runs(
                run_id, source, fetched_at, request_url, item_count,
                candidate_count, raw_path, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', NULL)
            """,
            (
                run_id,
                source,
                fetched_at.astimezone(UTC).isoformat(),
                request_url,
                item_count,
                candidate_count,
                str(raw_path),
            ),
        )
        self.connection.commit()
        return run_id

    def upsert_markets(self, markets: Iterable[Market], *, observed_at: datetime) -> int:
        timestamp = observed_at.astimezone(UTC).isoformat()
        rows = []
        for market in markets:
            rows.append(
                (
                    market.market_id,
                    market.question,
                    market.slug,
                    market.condition_id,
                    market.category,
                    int(market.active),
                    int(market.closed),
                    market.end_date.isoformat() if market.end_date else None,
                    market.resolution_source,
                    json.dumps(market.outcomes, ensure_ascii=False),
                    json.dumps([str(price) for price in market.outcome_prices]),
                    json.dumps(market.clob_token_ids),
                    json.dumps(market.raw, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    timestamp,
                )
            )
        self.connection.executemany(
            """
            INSERT INTO markets(
                market_id, question, slug, condition_id, category, active, closed,
                end_date, resolution_source, outcomes_json, prices_json,
                clob_token_ids_json, raw_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                question=excluded.question,
                slug=excluded.slug,
                condition_id=excluded.condition_id,
                category=excluded.category,
                active=excluded.active,
                closed=excluded.closed,
                end_date=excluded.end_date,
                resolution_source=excluded.resolution_source,
                outcomes_json=excluded.outcomes_json,
                prices_json=excluded.prices_json,
                clob_token_ids_json=excluded.clob_token_ids_json,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def market_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM markets").fetchone()
        return int(row[0]) if row else 0

