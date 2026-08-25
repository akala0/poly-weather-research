from __future__ import annotations

import gzip
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    raw_retention_days: int = 30
    compress_after_days: int = 2
    disk_warning_gb: float = 20.0
    projected_trimmed_gb_per_day: float = 2.2


def _partition_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name)
    except ValueError:
        return None


def _assert_within(path: Path, root: Path) -> None:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError(f"retention target escapes market raw root: {resolved}")


def apply_market_retention(
    data_dir: Path,
    *,
    config: RetentionConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compress and expire only raw Polymarket streams; aggregates are untouched."""
    config = config or RetentionConfig()
    current_date = (now or datetime.now(UTC)).astimezone(UTC).date()
    compressed: list[str] = []
    deleted: list[str] = []
    for source in ("polymarket_clob_websocket", "polymarket_book_checkpoints"):
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
    return {
        "config": asdict(config),
        "compressed": compressed,
        "deleted": deleted,
        "aggregate_data_retained": True,
    }


def disk_capacity_status(
    data_dir: Path,
    *,
    warning_gb: float = 20.0,
    projected_trimmed_gb_per_day: float = 2.2,
) -> dict[str, Any]:
    usage = shutil.disk_usage(data_dir.resolve())
    free_gb = usage.free / 1024**3
    root = data_dir / "raw" / "polymarket_clob_websocket"
    daily_sizes: list[int] = []
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
            daily_sizes.append(sum(item.stat().st_size for item in partition.rglob("*") if item.is_file()))
    average_bytes = sum(daily_sizes) / len(daily_sizes) if daily_sizes else 0.0
    estimated_days = usage.free / average_bytes if average_bytes > 0 else None
    return {
        "disk_free_gb": round(free_gb, 2),
        "disk_warning_threshold_gb": warning_gb,
        "disk_warning": free_gb < warning_gb,
        "recent_market_archive_bytes_per_day": round(average_bytes),
        "estimated_capture_days_at_recent_rate": (
            round(estimated_days, 1) if estimated_days is not None else None
        ),
        "projected_trimmed_market_archive_gb_per_day": projected_trimmed_gb_per_day,
        "estimated_capture_days_at_projected_trimmed_rate": round(
            free_gb / projected_trimmed_gb_per_day, 1
        ),
    }
