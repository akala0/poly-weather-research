from pathlib import Path

from poly_weather.config import load_settlement_registry
from poly_weather.domain import VerificationStatus


def test_example_registry_has_only_evidence_verified_cities_enabled() -> None:
    registry = load_settlement_registry(Path("configs/settlements.example.json"))

    assert len(registry.specs) == 4
    assert registry.by_key("los-angeles-daily-high-research-seed").station_id == "KLAX"
    assert registry.by_key("new-york-daily-high-research-seed").status is VerificationStatus.VERIFIED
    assert registry.by_key("los-angeles-daily-high-research-seed").status is VerificationStatus.VERIFIED
    assert registry.by_key("chicago-daily-high-research-seed").status is VerificationStatus.UNVERIFIED
    assert registry.by_key("miami-daily-high-research-seed").status is VerificationStatus.UNVERIFIED
