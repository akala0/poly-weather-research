import json
from datetime import date
from pathlib import Path

import pytest

from poly_weather.high_frequency_audit import load_wrh_historical_observations


def _batch(path: Path, *, mode: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "collection_mode": mode,
                "payload": {
                    "STATION": [
                        {
                            "OBSERVATIONS": {
                                "date_time": [
                                    "2026-08-24T03:59:00Z",
                                    "2026-08-24T04:00:00Z",
                                ],
                                "air_temp_set_1": [70.0, 71.25],
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_wrh_history_loader_converts_to_local_day_and_filters(tmp_path: Path) -> None:
    path = tmp_path / "raw" / "wrh_history_batches" / "KLGA" / "batch.json"
    _batch(path, mode="historical_backfill")
    rows = load_wrh_historical_observations(
        tmp_path,
        station_id="KLGA",
        timezone="America/New_York",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 24),
    )
    assert len(rows) == 1
    assert rows[0].valid.isoformat() == "2026-08-24T00:00:00"
    assert rows[0].temperature_f == 71.25


def test_wrh_history_loader_refuses_realtime_batch(tmp_path: Path) -> None:
    path = tmp_path / "raw" / "wrh_history_batches" / "KLGA" / "batch.json"
    _batch(path, mode="realtime")
    with pytest.raises(ValueError, match="lacks historical provenance"):
        load_wrh_historical_observations(
            tmp_path,
            station_id="KLGA",
            timezone="America/New_York",
        )
