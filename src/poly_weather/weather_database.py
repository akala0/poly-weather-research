"""Fail-closed inspection and versioned recovery for the weather warehouse."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from poly_weather.research_store import ResearchWarehouse
from poly_weather.runtime_safety import (
    StatusIntegrityError,
    atomic_json_write,
    read_json_with_fallback,
)
from poly_weather.weather_provenance import collection_mode

WEATHER_DB_MEMORY_LIMIT = "256MB"
WEATHER_DB_MAX_TEMP_DIRECTORY_SIZE = "4GB"
WEATHER_DB_THREADS = 1


def _temp_directory(data_dir: Path) -> Path:
    path = data_dir / "tmp" / "duckdb-weather-integrity"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_size(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        columns = [item[0] for item in connection.description or ()]
        values = connection.fetchall()
    except duckdb.Error as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "available",
        "columns": columns,
        "rows": [dict(zip(columns, row, strict=False)) for row in values],
    }


def inspect_weather_database(
    path: Path,
    *,
    data_dir: Path | None = None,
    memory_limit: str = WEATHER_DB_MEMORY_LIMIT,
) -> dict[str, Any]:
    """Inspect a weather DB through an independent read-only connection.

    This function never runs CHECKPOINT, VACUUM, DDL, or any write statement.
    A missing database is a normal pre-start state; a present but unreadable
    database is explicitly returned as damaged so the caller can select a
    versioned replacement.
    """

    path = path.resolve()
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "ok": True,
            "mode": "missing_prestart",
            "mutations_attempted": False,
        }
    temp_directory = _temp_directory(data_dir or path.parent)
    config = {
        "memory_limit": memory_limit,
        "temp_directory": str(temp_directory.resolve()),
        "threads": str(WEATHER_DB_THREADS),
        "max_temp_directory_size": WEATHER_DB_MAX_TEMP_DIRECTORY_SIZE,
    }
    result: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "ok": False,
        "mode": "read_only_integrity_check",
        "mutations_attempted": False,
        "config": config,
        "database_size": None,
        "tables": {},
        "weather_max": None,
        "random_rows": [],
        "required_tables": [
            "main.weather_stream_events",
            "main.weather_stream_runs",
        ],
    }
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(str(path), read_only=True, config=config)
        result["duckdb_version"] = str(connection.execute("SELECT version()").fetchone()[0])
        connection.execute("PRAGMA database_size")
        result["database_size"] = _database_size(connection)
        table_rows = connection.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
            ORDER BY table_schema, table_name
            """
        ).fetchall()
        for schema, table in table_rows:
            qualified = f'"{schema}"."{table}"'
            count = connection.execute(f"SELECT COUNT(*) FROM {qualified}").fetchone()[0]
            result["tables"][f"{schema}.{table}"] = int(count)
        missing_tables = [
            table
            for table in result["required_tables"]
            if table not in result["tables"]
        ]
        if missing_tables:
            result["missing_required_tables"] = missing_tables
            result["error"] = "required weather tables are missing"
            return result
        if "main.weather_stream_events" in result["tables"]:
            result["weather_max"] = connection.execute(
                """
                SELECT MAX(received_at), MAX(received_at_ns), COUNT(*)
                FROM main.weather_stream_events
                """
            ).fetchone()
            result["weather_max"] = {
                "max_received_at": (
                    result["weather_max"][0].isoformat()
                    if result["weather_max"][0] is not None
                    else None
                ),
                "max_received_at_ns": (
                    int(result["weather_max"][1])
                    if result["weather_max"][1] is not None
                    else None
                ),
                "row_count": int(result["weather_max"][2]),
            }
            result["random_rows"] = [
                list(row)
                for row in connection.execute(
                    """
                    SELECT run_id, sequence, received_at, provider, product, station_id
                    FROM main.weather_stream_events
                    ORDER BY hash(run_id || ':' || CAST(sequence AS VARCHAR))
                    LIMIT 3
                    """
                ).fetchall()
            ]
        # A second independent query makes the read visibility explicit while
        # remaining strictly read-only.  Do not issue CHECKPOINT on this path.
        connection.execute("SELECT 1").fetchone()
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                result.setdefault("close_error", f"{type(exc).__name__}: {exc}")
    return result


def _weather_archive_paths(raw_root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                *raw_root.glob("*/events.jsonl"),
                *raw_root.glob("*/events.jsonl.gz"),
            )
        )
    )


def iter_archived_weather_events(
    raw_root: Path,
    *,
    stats: dict[str, int] | None = None,
) -> Iterator[Any]:
    """Yield valid raw weather envelopes without loading the archive in memory."""

    counters = stats if stats is not None else {}
    for key in (
        "archive_files_seen",
        "lines_seen",
        "valid_rows",
        "invalid_rows",
        "unreadable_archives",
    ):
        counters.setdefault(key, 0)

    from poly_weather.weather_stream import WeatherEvent

    for path in _weather_archive_paths(raw_root):
        counters["archive_files_seen"] += 1
        try:
            if path.suffix == ".gz":
                handle_context = gzip.open(path, mode="rt", encoding="utf-8")
            else:
                handle_context = path.open(encoding="utf-8")
            with handle_context as handle:
                for _line_number, line in enumerate(handle, start=1):
                    counters["lines_seen"] += 1
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        mode = collection_mode(row)
                        received_at_ns = int(row["received_at_ns"])
                        event = WeatherEvent(
                            run_id=str(row["run_id"]),
                            sequence=int(row["sequence"]),
                            received_at_ns=received_at_ns,
                            source_timestamp_ms=(
                                int(row["source_timestamp_ms"])
                                if row.get("source_timestamp_ms") is not None
                                else None
                            ),
                            provider=str(row.get("provider") or "unknown"),
                            product=str(row["product"]),
                            station_id=str(row["station_id"]).upper(),
                            temperature_c=(
                                float(row["temperature_c"])
                                if row.get("temperature_c") is not None
                                else None
                            ),
                            latency_ms=(
                                int(row["latency_ms"])
                                if row.get("latency_ms") is not None
                                else None
                            ),
                            raw=row.get("raw") if row.get("raw") is not None else {},
                            collection_mode=mode,
                        )
                        counters["valid_rows"] += 1
                        yield event
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        # A malformed raw line is counted by the rebuild caller;
                        # it must not make the new database look complete.
                        counters["invalid_rows"] += 1
                        continue
        except (OSError, EOFError, UnicodeError, gzip.BadGzipFile):
            counters["unreadable_archives"] += 1
            continue


def rebuild_weather_database(
    *,
    source_path: Path,
    raw_root: Path,
    target_path: Path,
    data_dir: Path,
    memory_limit: str = WEATHER_DB_MEMORY_LIMIT,
) -> dict[str, Any]:
    """Build a new versioned weather DB from raw envelopes, never replacing source."""

    source_path = source_path.resolve()
    target_path = target_path.resolve()
    if target_path == source_path:
        raise ValueError("weather recovery target must not equal source database")
    if target_path.exists() or target_path.with_suffix(target_path.suffix + ".wal").exists():
        raise FileExistsError(f"recovery target already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_directory = data_dir / "tmp" / "duckdb-weather-recovery"
    run_id = f"weather-recovery-{uuid4()}"
    inserted = 0
    archive_stats: dict[str, int] = {}
    archive_paths = _weather_archive_paths(raw_root)
    with ResearchWarehouse(
        target_path,
        memory_limit=memory_limit,
        temp_directory=temp_directory,
        threads=WEATHER_DB_THREADS,
        max_temp_directory_size=WEATHER_DB_MAX_TEMP_DIRECTORY_SIZE,
    ) as warehouse:
        warehouse.start_weather_stream_run(
            run_id=run_id,
            started_at=datetime.now(UTC),
            station_id="recovered_from_raw_archive",
        )
        batch: list[Any] = []
        for event in iter_archived_weather_events(raw_root, stats=archive_stats):
            batch.append(event)
            if len(batch) >= 16:
                inserted += warehouse.append_weather_stream_events(batch)
                batch.clear()
        if batch:
            inserted += warehouse.append_weather_stream_events(batch)
        if inserted == 0:
            raise RuntimeError(
                "weather recovery found no valid raw weather events; "
                "candidate database will not be selected"
            )
        warehouse.finish_weather_stream_run(
            run_id=run_id,
            finished_at=datetime.now(UTC),
            metrics={
                "mode": "versioned_raw_archive_rebuild",
                "inserted_rows": inserted,
                "source_path": str(source_path),
            },
        )
    verification = inspect_weather_database(
        target_path,
        data_dir=data_dir,
        memory_limit=memory_limit,
    )
    return {
        "mode": "versioned_raw_archive_rebuild",
        "source_path": str(source_path),
        "target_path": str(target_path),
        "source_preserved": source_path.exists(),
        "archive_path_count": len(archive_paths),
        "archive_stats": archive_stats,
        "inserted_rows": inserted,
        "invalid_or_unreadable_rows": (
            archive_stats.get("invalid_rows", 0)
            + archive_stats.get("unreadable_archives", 0)
        ),
        "verification": verification,
        "execution_enabled": False,
    }


def ensure_weather_database(
    data_dir: Path,
    *,
    memory_limit: str = WEATHER_DB_MEMORY_LIMIT,
) -> tuple[Path, dict[str, Any]]:
    """Return a verified path, selecting a versioned rebuild only when needed."""

    source_path = (data_dir / "weather_stream.duckdb").resolve()
    report_path = data_dir / "runtime" / "weather_database_recovery.json"
    existing_report: dict[str, Any] = {}
    if report_path.exists():
        try:
            loaded, _integrity, _source = read_json_with_fallback(report_path)
            existing_report = loaded
        except (OSError, StatusIntegrityError, TypeError, ValueError, json.JSONDecodeError):
            existing_report = {}
    candidate_text = existing_report.get("active_target_path")
    candidate: Path | None = None
    if candidate_text:
        proposed = Path(str(candidate_text)).resolve()
        if (
            proposed.parent == data_dir.resolve()
            and proposed.name.startswith("weather_stream.recovered.")
            and proposed.suffix == ".duckdb"
        ):
            candidate = proposed
    if candidate is not None and candidate.exists():
        candidate_check = inspect_weather_database(
            candidate, data_dir=data_dir, memory_limit=memory_limit
        )
        if candidate_check.get("ok"):
            result = {
                "mode": "using_verified_versioned_recovery",
                "source_check": existing_report.get("source_check"),
                "selected_path": str(candidate),
                "candidate_check": candidate_check,
                "source_preserved": source_path.exists(),
                "execution_enabled": False,
            }
            atomic_json_write(report_path, {**existing_report, **result})
            return candidate, result

    source_check = inspect_weather_database(
        source_path, data_dir=data_dir, memory_limit=memory_limit
    )
    if not source_check.get("exists"):
        result = {
            "mode": "missing_prestart",
            "selected_path": str(source_path),
            "source_check": source_check,
            "source_preserved": False,
            "execution_enabled": False,
        }
        atomic_json_write(report_path, result)
        return source_path, result
    if source_check.get("ok"):
        result = {
            "mode": "read_only_verified_source",
            "selected_path": str(source_path),
            "source_check": source_check,
            "source_preserved": True,
            "execution_enabled": False,
        }
        atomic_json_write(report_path, result)
        return source_path, result

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = data_dir / f"weather_stream.recovered.{stamp}.{uuid4().hex}.duckdb"
    recovery = rebuild_weather_database(
        source_path=source_path,
        raw_root=data_dir / "raw" / "weather_daemon",
        target_path=target,
        data_dir=data_dir,
        memory_limit=memory_limit,
    )
    if not recovery["verification"].get("ok"):
        raise RuntimeError("versioned weather database recovery failed verification")
    result = {
        **recovery,
        "mode": "selected_versioned_recovery",
        "selected_path": str(target.resolve()),
        "source_check": source_check,
        "active_target_path": str(target.resolve()),
        "recovered_at": datetime.now(UTC).isoformat(),
    }
    atomic_json_write(report_path, result)
    return target.resolve(), result


__all__ = [
    "WEATHER_DB_MEMORY_LIMIT",
    "WEATHER_DB_MAX_TEMP_DIRECTORY_SIZE",
    "WEATHER_DB_THREADS",
    "ensure_weather_database",
    "inspect_weather_database",
    "iter_archived_weather_events",
    "rebuild_weather_database",
]
