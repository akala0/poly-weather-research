from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from poly_weather.domain import (
    CalibrationSample,
    MarketPricePoint,
    MonitorSnapshot,
    PaperDecision,
    RollingEvaluation,
)
from poly_weather.signal_schema import (
    append_normalized_signal_snapshots,
    create_normalized_signal_schema,
)


class ResearchWarehouse:
    """DuckDB operational research store with reproducible Parquet exports."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(path))
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ResearchWarehouse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_samples (
                station_id VARCHAR NOT NULL,
                target_date DATE NOT NULL,
                lead_days INTEGER NOT NULL,
                model VARCHAR NOT NULL,
                forecast_high_f DOUBLE NOT NULL,
                observed_high_f DOUBLE NOT NULL,
                forecast_source VARCHAR NOT NULL,
                truth_source VARCHAR NOT NULL,
                truth_kind VARCHAR NOT NULL,
                ingested_at TIMESTAMPTZ NOT NULL,
                forecast_high_f_by_model JSON,
                PRIMARY KEY (station_id, target_date, lead_days, model, truth_source)
            );
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                run_id VARCHAR PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                station_id VARCHAR NOT NULL,
                lead_days INTEGER NOT NULL,
                model VARCHAR NOT NULL,
                metrics_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_price_points (
                event_id VARCHAR NOT NULL,
                event_slug VARCHAR NOT NULL,
                market_id VARCHAR NOT NULL,
                market_slug VARCHAR NOT NULL,
                token_id VARCHAR NOT NULL,
                outcome VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                price DECIMAL(18, 12) NOT NULL,
                source VARCHAR NOT NULL,
                ingested_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (token_id, observed_at)
            );
            CREATE INDEX IF NOT EXISTS market_price_lookup_idx
                ON market_price_points(event_slug, market_slug, observed_at);
            CREATE TABLE IF NOT EXISTS paper_decisions (
                decision_id VARCHAR PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                event_slug VARCHAR NOT NULL,
                market_slug VARCHAR NOT NULL,
                decision_time TIMESTAMPTZ NOT NULL,
                action VARCHAR NOT NULL,
                decision_json VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitor_snapshots (
                snapshot_id VARCHAR PRIMARY KEY,
                captured_at TIMESTAMPTZ NOT NULL,
                station_id VARCHAR NOT NULL,
                event_slug VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                snapshot_json VARCHAR NOT NULL
            );
            CREATE INDEX IF NOT EXISTS monitor_snapshot_lookup_idx
                ON monitor_snapshots(event_slug, captured_at);
            CREATE TABLE IF NOT EXISTS market_stream_runs (
                run_id VARCHAR PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                websocket_url VARCHAR NOT NULL,
                asset_slugs_json VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                metrics_json VARCHAR
            );
            CREATE TABLE IF NOT EXISTS market_stream_events (
                run_id VARCHAR NOT NULL,
                sequence BIGINT NOT NULL,
                received_at TIMESTAMPTZ NOT NULL,
                received_at_ns UBIGINT NOT NULL,
                source_timestamp_ms BIGINT,
                event_type VARCHAR NOT NULL,
                asset_id VARCHAR,
                market_id VARCHAR,
                market_slug VARCHAR,
                best_bid DECIMAL(18, 12),
                best_ask DECIMAL(18, 12),
                last_trade_price DECIMAL(18, 12),
                raw_json VARCHAR NOT NULL,
                bids_json VARCHAR,
                asks_json VARCHAR,
                book_complete BOOLEAN NOT NULL DEFAULT FALSE,
                upstream_status VARCHAR NOT NULL DEFAULT 'normal',
                upstream_incident_id VARCHAR,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS market_stream_asset_time_idx
                ON market_stream_events(asset_id, received_at);
            CREATE TABLE IF NOT EXISTS market_data_quality_windows (
                incident_id VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                incident_type VARCHAR NOT NULL,
                start_at TIMESTAMPTZ NOT NULL,
                end_at TIMESTAMPTZ,
                affected_components_json VARCHAR NOT NULL,
                affects_market_data BOOLEAN NOT NULL,
                affects_trading BOOLEAN NOT NULL,
                status VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                default_excluded BOOLEAN NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_stream_runs (
                run_id VARCHAR PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                station_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                metrics_json VARCHAR
            );
            CREATE TABLE IF NOT EXISTS weather_stream_events (
                run_id VARCHAR NOT NULL,
                sequence BIGINT NOT NULL,
                received_at TIMESTAMPTZ NOT NULL,
                received_at_ns UBIGINT NOT NULL,
                source_timestamp_ms BIGINT,
                provider VARCHAR NOT NULL,
                product VARCHAR NOT NULL,
                station_id VARCHAR NOT NULL,
                temperature_c DOUBLE,
                latency_ms BIGINT,
                raw_json VARCHAR NOT NULL,
                collection_mode VARCHAR NOT NULL DEFAULT 'realtime',
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS weather_stream_station_time_idx
                ON weather_stream_events(station_id, received_at);
            CREATE TABLE IF NOT EXISTS signal_stream_runs (
                run_id VARCHAR PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                event_slugs_json VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                metrics_json VARCHAR
            );
            """
        )
        create_normalized_signal_schema(self.connection)
        self.connection.execute(
            "ALTER TABLE market_stream_events ADD COLUMN IF NOT EXISTS bids_json VARCHAR"
        )
        self.connection.execute(
            "ALTER TABLE market_stream_events ADD COLUMN IF NOT EXISTS asks_json VARCHAR"
        )
        self.connection.execute(
            """
            ALTER TABLE market_stream_events
            ADD COLUMN IF NOT EXISTS book_complete BOOLEAN DEFAULT FALSE
            """
        )
        self.connection.execute(
            "ALTER TABLE market_stream_events "
            "ADD COLUMN IF NOT EXISTS upstream_status VARCHAR DEFAULT 'normal'"
        )
        self.connection.execute(
            "ALTER TABLE market_stream_events "
            "ADD COLUMN IF NOT EXISTS upstream_incident_id VARCHAR"
        )
        self.connection.execute(
            "ALTER TABLE calibration_samples "
            "ADD COLUMN IF NOT EXISTS forecast_high_f_by_model JSON"
        )
        self.connection.execute(
            "ALTER TABLE weather_stream_events "
            "ADD COLUMN IF NOT EXISTS collection_mode VARCHAR DEFAULT 'realtime'"
        )

    def upsert_samples(self, samples: Iterable[CalibrationSample]) -> int:
        rows = list(samples)
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO calibration_samples (
                station_id, target_date, lead_days, model, forecast_high_f,
                observed_high_f, forecast_source, truth_source, truth_kind,
                ingested_at, forecast_high_f_by_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, target_date, lead_days, model, truth_source)
            DO UPDATE SET
                forecast_high_f=excluded.forecast_high_f,
                observed_high_f=excluded.observed_high_f,
                forecast_source=excluded.forecast_source,
                truth_kind=excluded.truth_kind,
                ingested_at=excluded.ingested_at,
                forecast_high_f_by_model=excluded.forecast_high_f_by_model
            """,
            [
                (
                    row.station_id,
                    row.target_date,
                    row.lead_days,
                    row.model,
                    row.forecast_high_f,
                    row.observed_high_f,
                    row.forecast_source,
                    row.truth_source,
                    row.truth_kind,
                    row.ingested_at,
                    (
                        json.dumps(
                            row.forecast_high_f_by_model,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        if row.forecast_high_f_by_model is not None
                        else None
                    ),
                )
                for row in rows
            ],
        )
        return len(rows)

    def samples(
        self,
        *,
        station_id: str,
        lead_days: int,
        model: str,
    ) -> list[CalibrationSample]:
        result = self.connection.execute(
            """
            SELECT station_id, target_date, lead_days, model, forecast_high_f,
                   observed_high_f, forecast_source, truth_source, truth_kind, ingested_at,
                   forecast_high_f_by_model
            FROM calibration_samples
            WHERE station_id = ? AND lead_days = ? AND model = ?
            ORDER BY target_date
            """,
            [station_id, lead_days, model],
        ).fetchall()
        return [
            CalibrationSample(
                station_id=row[0],
                target_date=row[1],
                lead_days=row[2],
                model=row[3],
                forecast_high_f=row[4],
                observed_high_f=row[5],
                forecast_source=row[6],
                truth_source=row[7],
                truth_kind=row[8],
                ingested_at=row[9],
                forecast_high_f_by_model=(
                    json.loads(row[10]) if isinstance(row[10], str) else row[10]
                ),
            )
            for row in result
        ]

    def record_evaluation(self, evaluation: RollingEvaluation) -> str:
        run_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id,
                datetime.now(UTC),
                evaluation.station_id,
                evaluation.lead_days,
                evaluation.model,
                evaluation.model_dump_json(),
            ],
        )
        return run_id

    def upsert_price_points(
        self,
        *,
        event_id: str,
        event_slug: str,
        market_id: str,
        market_slug: str,
        outcome: str,
        source: str,
        points: Iterable[MarketPricePoint],
        ingested_at: datetime,
    ) -> int:
        rows = list(points)
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO market_price_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (token_id, observed_at) DO UPDATE SET
                event_id=excluded.event_id,
                event_slug=excluded.event_slug,
                market_id=excluded.market_id,
                market_slug=excluded.market_slug,
                outcome=excluded.outcome,
                price=excluded.price,
                source=excluded.source,
                ingested_at=excluded.ingested_at
            """,
            [
                (
                    event_id,
                    event_slug,
                    market_id,
                    market_slug,
                    point.token_id,
                    outcome,
                    point.timestamp.astimezone(UTC),
                    point.price,
                    source,
                    ingested_at.astimezone(UTC),
                )
                for point in rows
            ],
        )
        return len(rows)

    def price_at_or_before(
        self,
        *,
        token_id: str,
        decision_time: datetime,
        max_age: timedelta,
    ) -> MarketPricePoint | None:
        decision_utc = decision_time.astimezone(UTC)
        row = self.connection.execute(
            """
            SELECT token_id, observed_at, price
            FROM market_price_points
            WHERE token_id = ? AND observed_at <= ? AND observed_at >= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [token_id, decision_utc, decision_utc - max_age],
        ).fetchone()
        if row is None:
            return None
        return MarketPricePoint(
            token_id=row[0],
            timestamp=row[1].astimezone(UTC),
            price=Decimal(str(row[2])),
        )

    def record_paper_decision(self, decision: PaperDecision) -> str:
        decision_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO paper_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                decision_id,
                datetime.now(UTC),
                decision.event_slug,
                decision.market_slug,
                decision.decision_time.astimezone(UTC),
                decision.action.value,
                decision.model_dump_json(),
            ],
        )
        return decision_id

    def record_monitor_snapshot(self, snapshot: MonitorSnapshot) -> str:
        snapshot_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO monitor_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            [
                snapshot_id,
                snapshot.captured_at.astimezone(UTC),
                snapshot.station_id,
                snapshot.event_slug,
                snapshot.status.value,
                snapshot.model_dump_json(),
            ],
        )
        return snapshot_id

    def start_market_stream_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        websocket_url: str,
        asset_slugs: dict[str, str],
    ) -> None:
        import json

        self.connection.execute(
            "INSERT INTO market_stream_runs VALUES (?, ?, NULL, ?, ?, 'running', NULL)",
            [
                run_id,
                started_at.astimezone(UTC),
                websocket_url,
                json.dumps(asset_slugs, ensure_ascii=False, separators=(",", ":")),
            ],
        )

    def append_market_stream_events(self, records: Iterable[Any]) -> int:
        import json

        rows = list(records)
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO market_stream_events (
                run_id,
                sequence,
                received_at,
                received_at_ns,
                source_timestamp_ms,
                event_type,
                asset_id,
                market_id,
                market_slug,
                best_bid,
                best_ask,
                last_trade_price,
                raw_json,
                bids_json,
                asks_json,
                book_complete,
                upstream_status,
                upstream_incident_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, sequence) DO NOTHING
            """,
            [
                (
                    record.run_id,
                    record.sequence,
                    datetime.fromtimestamp(record.received_at_ns / 1_000_000_000, tz=UTC),
                    record.received_at_ns,
                    record.source_timestamp_ms,
                    record.event_type,
                    record.asset_id,
                    record.market_id,
                    record.market_slug,
                    record.best_bid,
                    record.best_ask,
                    record.last_trade_price,
                    json.dumps(record.raw, ensure_ascii=False, separators=(",", ":")),
                    (
                        json.dumps(record.bids, ensure_ascii=False, separators=(",", ":"))
                        if record.bids is not None
                        else None
                    ),
                    (
                        json.dumps(record.asks, ensure_ascii=False, separators=(",", ":"))
                        if record.asks is not None
                        else None
                    ),
                    record.book_complete,
                    record.upstream_status,
                    record.upstream_incident_id,
                )
                for record in rows
            ],
        )
        return len(rows)

    def upsert_market_data_quality_windows(self, windows: Iterable[Any]) -> int:
        import json

        rows = list(windows)
        if not rows:
            return 0
        now = datetime.now(UTC)
        self.connection.executemany(
            """
            INSERT INTO market_data_quality_windows (
                incident_id, title, incident_type, start_at, end_at,
                affected_components_json, affects_market_data, affects_trading,
                status, source_url, default_excluded, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (incident_id) DO UPDATE SET
                title=excluded.title,
                incident_type=excluded.incident_type,
                start_at=excluded.start_at,
                end_at=excluded.end_at,
                affected_components_json=excluded.affected_components_json,
                affects_market_data=excluded.affects_market_data,
                affects_trading=excluded.affects_trading,
                status=excluded.status,
                source_url=excluded.source_url,
                default_excluded=excluded.default_excluded,
                updated_at=excluded.updated_at
            """,
            [
                (
                    row.incident_id,
                    row.title,
                    row.incident_type,
                    row.start_at,
                    row.end_at,
                    json.dumps(row.affected_components, separators=(",", ":")),
                    row.affects_market_data,
                    row.affects_trading,
                    row.status,
                    row.source_url,
                    row.default_excluded,
                    now,
                )
                for row in rows
            ],
        )
        return len(rows)

    def finish_market_stream_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        metrics: dict[str, Any],
    ) -> None:
        import json

        self.connection.execute(
            """
            UPDATE market_stream_runs
            SET finished_at = ?, status = 'stopped', metrics_json = ?
            WHERE run_id = ?
            """,
            [
                finished_at.astimezone(UTC),
                json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                run_id,
            ],
        )

    def start_weather_stream_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        station_id: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO weather_stream_runs VALUES (?, ?, NULL, ?, 'running', NULL)",
            [run_id, started_at.astimezone(UTC), station_id],
        )

    def append_weather_stream_events(self, events: Iterable[Any]) -> int:
        import json

        rows = list(events)
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO weather_stream_events (
                run_id, sequence, received_at, received_at_ns,
                source_timestamp_ms, provider, product, station_id,
                temperature_c, latency_ms, raw_json, collection_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, sequence) DO NOTHING
            """,
            [
                (
                    event.run_id,
                    event.sequence,
                    datetime.fromtimestamp(event.received_at_ns / 1_000_000_000, tz=UTC),
                    event.received_at_ns,
                    event.source_timestamp_ms,
                    event.provider,
                    event.product,
                    event.station_id,
                    event.temperature_c,
                    event.latency_ms,
                    json.dumps(event.raw, ensure_ascii=False, separators=(",", ":")),
                    event.collection_mode,
                )
                for event in rows
            ],
        )
        return len(rows)

    def finish_weather_stream_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        metrics: dict[str, Any],
    ) -> None:
        import json

        self.connection.execute(
            """
            UPDATE weather_stream_runs
            SET finished_at = ?, status = 'stopped', metrics_json = ?
            WHERE run_id = ?
            """,
            [
                finished_at.astimezone(UTC),
                json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                run_id,
            ],
        )

    def start_signal_stream_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        event_slugs: list[str],
    ) -> None:
        import json

        self.connection.execute(
            "INSERT INTO signal_stream_runs VALUES (?, ?, NULL, ?, 'running', NULL)",
            [
                run_id,
                started_at.astimezone(UTC),
                json.dumps(event_slugs, ensure_ascii=False, separators=(",", ":")),
            ],
        )

    def append_signal_snapshots(self, snapshots: Iterable[dict[str, Any]]) -> int:
        return append_normalized_signal_snapshots(self.connection, snapshots)

    def checkpoint_signal_database(self) -> None:
        """Reclaim committed storage without changing snapshot retention."""
        self.connection.execute("CHECKPOINT")
        self.connection.execute("VACUUM")

    def finish_signal_stream_run(
        self,
        *,
        run_id: str,
        finished_at: datetime,
        metrics: dict[str, Any],
    ) -> None:
        import json

        self.connection.execute(
            """
            UPDATE signal_stream_runs
            SET finished_at = ?, status = 'stopped', metrics_json = ?
            WHERE run_id = ?
            """,
            [
                finished_at.astimezone(UTC),
                json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                run_id,
            ],
        )

    def export_samples_parquet(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection.sql(
            "SELECT * FROM calibration_samples ORDER BY station_id, target_date, lead_days, model"
        ).write_parquet(str(path), overwrite=True)
        return path

    def status(self) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT COUNT(*), MIN(target_date), MAX(target_date) FROM calibration_samples"
        ).fetchone()
        price_count = self.connection.execute("SELECT COUNT(*) FROM market_price_points").fetchone()
        decision_count = self.connection.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()
        monitor_count = self.connection.execute("SELECT COUNT(*) FROM monitor_snapshots").fetchone()
        stream_count = self.connection.execute("SELECT COUNT(*) FROM market_stream_events").fetchone()
        weather_stream_count = self.connection.execute(
            "SELECT COUNT(*) FROM weather_stream_events"
        ).fetchone()
        signal_count = self.connection.execute("SELECT COUNT(*) FROM signal_snapshots").fetchone()
        return {
            "sample_count": int(row[0]),
            "date_min": row[1].isoformat() if row[1] else None,
            "date_max": row[2].isoformat() if row[2] else None,
            "database": str(self.path.resolve()),
            "price_point_count": int(price_count[0]) if price_count else 0,
            "paper_decision_count": int(decision_count[0]) if decision_count else 0,
            "monitor_snapshot_count": int(monitor_count[0]) if monitor_count else 0,
            "market_stream_event_count": int(stream_count[0]) if stream_count else 0,
            "weather_stream_event_count": (
                int(weather_stream_count[0]) if weather_stream_count else 0
            ),
            "signal_snapshot_count": int(signal_count[0]) if signal_count else 0,
        }
