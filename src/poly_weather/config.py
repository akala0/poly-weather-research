from __future__ import annotations

from pathlib import Path

from poly_weather.domain import SettlementRegistry


def load_settlement_registry(path: Path) -> SettlementRegistry:
    return SettlementRegistry.model_validate_json(path.read_text(encoding="utf-8"))

