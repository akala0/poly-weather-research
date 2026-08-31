from __future__ import annotations

import gzip
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from poly_weather.signal_schema import signal_schema_is_normalized

RAW_RETENTION_SOURCES = (
    "polymarket_clob_websocket",
    "polymarket_book_checkpoints",
    "signal_snapshot",
)


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    raw_retention_days: int = 30
    compress_after_days: int = 2
    disk_warning_gb: float = 20.0
    projected_trimmed_gb_per_day: float = 2.2
    # Disabled until normalized-schema size measurements justify row expiry.
    signal_snapshot_retention_days: int | None = None


def _partition_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name)
    except ValueError:
        return None


def _assert_within(path: Path, root: Path) -> None:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError(f"retention target escapes configured raw root: {resolved}")


def file_storage_bytes(path: Path) -> tuple[int, int]:
    """Return logical and physically allocated bytes, including NTFS compression."""
    logical = path.stat().st_size
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        compressed_size = kernel32.GetCompressedFileSizeW
        compressed_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        compressed_size.restype = wintypes.DWORD
        high = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        low = compressed_size(str(path.resolve()), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
            return logical, logical
        return logical, (int(high.value) << 32) | int(low)
    blocks = getattr(path.stat(), "st_blocks", None)
    return logical, int(blocks) * 512 if blocks is not None else logical


def directory_storage_bytes(path: Path) -> tuple[int, int]:
    logical = 0
    physical = 0
    if not path.exists():
        return logical, physical
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        item_logical, item_physical = file_storage_bytes(item)
        logical += item_logical
        physical += item_physical
    return logical, physical


def apply_market_retention(
    data_dir: Path,
    *,
    config: RetentionConfig | None = None,
    now: datetime | None = None,
    maintain_signal_database_online: bool = True,
) -> dict[str, Any]:
    """Compress/expire raw streams without touching protected aggregates.

    A resident signal writer owns its DuckDB connection.  Supervisors should
    pass ``maintain_signal_database_online=False`` so a periodic retention
    pass cannot race that writer with CHECKPOINT/VACUUM.
    """
    config = config or RetentionConfig()
    current_date = (now or datetime.now(UTC)).astimezone(UTC).date()
    compressed: list[str] = []
    deleted: list[str] = []
    for source in RAW_RETENTION_SOURCES:
        root = data_dir / "raw" / source
        if not root.exists():
            continue
        for partition in root.iterdir():
            partition_date = _partition_date(partition)
            if partition_date is None or not partition.is_dir():
                continue
            _assert_within(partition, root)
            age_days = (current_date - partition_date).days
            if age_days > config.raw_retention_days:
                for child in partition.iterdir():
                    _assert_within(child, root)
                    if child.is_file():
                        child.unlink()
                partition.rmdir()
                deleted.append(str(partition))
                continue
            if age_days < config.compress_after_days:
                continue
            for jsonl in partition.glob("*.jsonl"):
                _assert_within(jsonl, root)
                target = jsonl.with_suffix(jsonl.suffix + ".gz")
                _assert_within(target, root)
                if target.exists():
                    continue
                temporary = target.with_suffix(target.suffix + ".tmp")
                with jsonl.open("rb") as source_handle, gzip.open(
                    temporary, "wb", compresslevel=6
                ) as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                temporary.replace(target)
                jsonl.unlink()
                compressed.append(str(target))
    signal_database = (
        maintain_signal_database(
            data_dir / "signal_stream.duckdb",
            retention_days=config.signal_snapshot_retention_days,
        )
        if maintain_signal_database_online
        else {
            "state": "skipped_online_writer_owns_database",
            "path": str((data_dir / "signal_stream.duckdb").resolve()),
            "deleted": 0,
            "vacuumed": False,
        }
    )
    return {
        "config": asdict(config),
        "compressed": compressed,
        "deleted": deleted,
        "aggregate_data_retained": True,
        "raw_retention_sources": list(RAW_RETENTION_SOURCES),
        "t7_evidence_tree_retained": str(
            (data_dir / "raw" / "no_forward_validation").resolve()
        ),
        "signal_database": signal_database,
    }


def maintain_signal_database(
    path: Path,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Checkpoint normalized signals; optional expiry always preserves critical rows."""
    if not path.exists():
        return {"state": "not_found", "path": str(path.resolve()), "deleted": 0}
    try:
        connection = duckdb.connect(str(path))
    except duckdb.IOException as exc:
        return {
            "state": "locked",
            "path": str(path.resolve()),
            "deleted": 0,
            "error": str(exc),
        }
    deleted = 0
    try:
        if not signal_schema_is_normalized(connection):
            connection.execute("CHECKPOINT")
            return {
                "state": "legacy_schema",
                "path": str(path.resolve()),
                "deleted": 0,
                "vacuumed": False,
            }
        if retention_days is not None:
            cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
            connection.execute(
                """
                CREATE TEMP TABLE expirable_signal_snapshots AS
                SELECT s.snapshot_id
                FROM signal_snapshots AS s
                WHERE s.generated_at < ?
                  AND s.status = 'healthy'
                  AND NOT EXISTS (
                      SELECT 1 FROM signal_bucket_observations AS b
                      WHERE b.snapshot_id = s.snapshot_id
                        AND b.warming_window_no
                  )
                """,
                [cutoff],
            )
            deleted = int(
                connection.execute(
                    "SELECT COUNT(*) FROM expirable_signal_snapshots"
                ).fetchone()[0]
            )
            if deleted:
                connection.execute(
                    """
                    DELETE FROM signal_snapshot_reasons
                    WHERE snapshot_id IN (SELECT snapshot_id FROM expirable_signal_snapshots);
                    DELETE FROM signal_bucket_observations
                    WHERE snapshot_id IN (SELECT snapshot_id FROM expirable_signal_snapshots);
                    DELETE FROM signal_snapshots
                    WHERE snapshot_id IN (SELECT snapshot_id FROM expirable_signal_snapshots);
                    """
                )
        connection.execute("CHECKPOINT")
        connection.execute("VACUUM")
        return {
            "state": "maintained",
            "path": str(path.resolve()),
            "retention_days": retention_days,
            "deleted": deleted,
            "vacuumed": True,
            "critical_rows_preserved": True,
        }
    finally:
        connection.close()


def disk_capacity_status(
    data_dir: Path,
    *,
    warning_gb: float = 20.0,
    projected_trimmed_gb_per_day: float = 2.2,
) -> dict[str, Any]:
    usage = shutil.disk_usage(data_dir.resolve())
    free_gb = usage.free / 1024**3
    root = data_dir / "raw" / "polymarket_clob_websocket"
    daily_logical_sizes: list[int] = []
    daily_physical_sizes: list[int] = []
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=7)
    if root.exists():
        for partition in root.iterdir():
            partition_date = _partition_date(partition)
            if (
                partition_date is None
                or partition_date < cutoff
                or partition_date >= today
                or not partition.is_dir()
            ):
                continue
            logical, physical = directory_storage_bytes(partition)
            daily_logical_sizes.append(logical)
            daily_physical_sizes.append(physical)
    average_logical_bytes = (
        sum(daily_logical_sizes) / len(daily_logical_sizes)
        if daily_logical_sizes
        else 0.0
    )
    average_physical_bytes = (
        sum(daily_physical_sizes) / len(daily_physical_sizes)
        if daily_physical_sizes
        else 0.0
    )
    compression_ratio = (
        average_logical_bytes / average_physical_bytes
        if average_physical_bytes > 0
        else 1.0
    )
    estimated_days = (
        usage.free / average_physical_bytes if average_physical_bytes > 0 else None
    )
    projected_physical_gb_per_day = projected_trimmed_gb_per_day / max(
        1.0, compression_ratio
    )
    signal_path = data_dir / "signal_stream.duckdb"
    signal_logical, signal_physical = (
        file_storage_bytes(signal_path) if signal_path.exists() else (0, 0)
    )
    return {
        "disk_free_gb": round(free_gb, 2),
        "disk_warning_threshold_gb": warning_gb,
        "disk_warning": free_gb < warning_gb,
        # Backward-compatible key now uses the capacity-relevant physical rate.
        "recent_market_archive_bytes_per_day": round(average_physical_bytes),
        "recent_market_archive_logical_bytes_per_day": round(average_logical_bytes),
        "recent_market_archive_physical_bytes_per_day": round(average_physical_bytes),
        "recent_market_archive_compression_ratio": round(compression_ratio, 3),
        "estimated_capture_days_at_recent_rate": (
            round(estimated_days, 1) if estimated_days is not None else None
        ),
        "projected_trimmed_market_archive_gb_per_day": round(
            projected_physical_gb_per_day, 3
        ),
        "projected_trimmed_market_archive_logical_gb_per_day": (
            projected_trimmed_gb_per_day
        ),
        "projected_trimmed_market_archive_physical_gb_per_day": round(
            projected_physical_gb_per_day, 3
        ),
        "projected_vs_recent_logical_rate_ratio": (
            round(
                projected_trimmed_gb_per_day
                / (average_logical_bytes / 1024**3),
                3,
            )
            if average_logical_bytes > 0
            else None
        ),
        "signal_database_logical_bytes": signal_logical,
        "signal_database_physical_bytes": signal_physical,
        "signal_database_compression_ratio": round(
            signal_logical / signal_physical if signal_physical > 0 else 1.0,
            3,
        ),
        "estimated_capture_days_at_projected_trimmed_rate": round(
            free_gb / projected_physical_gb_per_day, 1
        ),
    }
