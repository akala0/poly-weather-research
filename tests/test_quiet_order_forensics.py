"""Failure-mode tests for the QUIET v2 zero-fill forensic audit."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.market_regime import RegimeState, RegimeTransition, ThresholdTier
from poly_weather.quiet_order_forensics import (
    CANCELLED_BEFORE_LATER_TOUCH,
    INVALID_RULE_PROVENANCE,
    NEVER_TOUCHED,
    NO_LATER_HEALTHY_BOOK,
    OTHER_WITH_EVIDENCE,
    TOUCHED_QUEUE_NOT_CLEARED,
    TRUE_ZERO_OPPOSITE_TRADE,
    UNKNOWN_TAPE_GAP,
    QuietLifecycleCollector,
    QuoteObservation,
    RuleProvenance,
    _rule_at,
    _small_size_sensitivity,
    annotate_episode_book_observations,
    audit_order_path,
    collect_archived_order_quotes,
    cross_bucket_coverage_sensitivity,
    render_quiet_order_forensics_report,
)
from poly_weather.shadow_orders import BookSnapshot, ShadowSide, TradeEvent

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
KNOWN_RULE = RuleProvenance(tick_size=Decimal("0.01"), min_order_size=Decimal("5"))


def _order(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "order_id": "quiet-1",
        "threshold_tier": "neutral",
        "event_id": "event-1",
        "market_id": "event-1:Yes",
        "token_id": "token-1",
        "station_id": "KLAX",
        "market_day": "2026-08-28",
        "submitted_at": BASE,
        "cancelled_at": BASE + timedelta(minutes=4),
        "side": "BUY",
        "limit_price": "0.40",
        "requested_shares": "10",
        "remaining_shares": "10",
        "requested_usd": "4",
        "better_level_shares": "0",
        "volume_ahead": "0",
    }
    values.update(overrides)
    return values


def _quote(
    minute: float,
    *,
    bid: str = "0.39",
    ask: str = "0.41",
    healthy: bool = True,
) -> QuoteObservation:
    return QuoteObservation(
        timestamp=BASE + timedelta(minutes=minute),
        event_id="event-1",
        market_id="event-1:Yes",
        token_id="token-1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        healthy=healthy,
    )


def _trade(
    minute: float,
    *,
    price: str = "0.40",
    size: str = "10",
    sequence: int | None = 1,
) -> TradeEvent:
    return TradeEvent(
        timestamp=BASE + timedelta(minutes=minute),
        asset_id="token-1",
        side=ShadowSide.SELL,
        price=Decimal(price),
        size=Decimal(size),
        event_id="trade-1",
        sequence=sequence,
    )


def test_unknown_minimum_is_not_silently_treated_as_zero() -> None:
    row = audit_order_path(
        _order(), quotes=[_quote(1, ask="0.40")], rule_provenance=RuleProvenance(tick_size=Decimal("0.01"))
    )
    assert row["primary_reason"] == INVALID_RULE_PROVENANCE
    assert row["upper_bound_eligibility"]["touch_upper_bound"] is False


def test_no_later_healthy_book_is_distinct_from_no_touch() -> None:
    row = audit_order_path(_order(), quotes=[_quote(1, healthy=False)], rule_provenance=KNOWN_RULE)
    assert row["primary_reason"] == NO_LATER_HEALTHY_BOOK


def test_never_touch_is_a_quote_path_finding() -> None:
    row = audit_order_path(
        _order(), quotes=[_quote(0), _quote(1), _quote(2)], rule_provenance=KNOWN_RULE
    )
    assert row["primary_reason"] == NEVER_TOUCHED
    assert row["submit_book"]["spread"] == Decimal("0.02")
    assert row["book_path"]["later_best_ask_low"] == Decimal("0.41")
    assert row["book_path"]["nearest_distance_to_limit"] == Decimal("0.01")


def test_touch_with_unknown_tape_is_not_a_queue_failure() -> None:
    row = audit_order_path(
        _order(),
        quotes=[_quote(1, ask="0.40")],
        rule_provenance=KNOWN_RULE,
        tape_known=False,
        tape_status_reason="archive_gap",
    )
    assert row["primary_reason"] == UNKNOWN_TAPE_GAP


def test_touch_with_no_opposite_trade_is_explicit_zero_tape_volume() -> None:
    row = audit_order_path(
        _order(), quotes=[_quote(1, ask="0.40")], rule_provenance=KNOWN_RULE
    )
    assert row["primary_reason"] == TRUE_ZERO_OPPOSITE_TRADE


def test_touch_with_insufficient_queue_consumption_is_separate() -> None:
    row = audit_order_path(
        _order(volume_ahead="5"),
        quotes=[_quote(1, ask="0.40")],
        trades=[_trade(1, size="5")],
        rule_provenance=KNOWN_RULE,
    )
    assert row["primary_reason"] == TOUCHED_QUEUE_NOT_CLEARED
    assert row["queue_path"]["queue_remaining_shares"] == Decimal("0")


def test_queue_clear_is_an_upper_bound_not_an_execution_claim() -> None:
    row = audit_order_path(
        _order(volume_ahead="5"),
        quotes=[_quote(1, ask="0.40")],
        trades=[_trade(1, price="0.39", size="15")],
        rule_provenance=KNOWN_RULE,
    )
    assert row["primary_reason"] == OTHER_WITH_EVIDENCE
    assert row["upper_bound_eligibility"]["queue_upper_bound"] is True
    assert row["upper_bound_eligibility"]["conservative_fill"] is True


def test_same_second_ambiguity_blocks_only_conservative_layer() -> None:
    row = audit_order_path(
        _order(),
        quotes=[_quote(1, ask="0.40")],
        trades=[
            _trade(1, price="0.39", size="5", sequence=None),
            _trade(1, price="0.39", size="5", sequence=2),
        ],
        rule_provenance=KNOWN_RULE,
    )
    assert row["upper_bound_eligibility"]["queue_upper_bound"] is True
    assert row["upper_bound_eligibility"]["conservative_fill"] is False


def test_later_touch_after_cancel_is_not_backfilled_into_live_fill() -> None:
    row = audit_order_path(
        _order(),
        quotes=[_quote(1), _quote(5, ask="0.40")],
        rule_provenance=KNOWN_RULE,
    )
    assert row["primary_reason"] == CANCELLED_BEFORE_LATER_TOUCH
    assert row["post_cancel_30m"]["touch_observed"] is True


def test_counterfactual_horizons_do_not_extend_live_timeout() -> None:
    row = audit_order_path(
        _order(),
        quotes=[_quote(6, ask="0.40")],
        rule_provenance=KNOWN_RULE,
        cutoff=BASE + timedelta(minutes=20),
    )
    assert row["counterfactual_touch_only"]["5"]["touch_observed"] is False
    assert row["counterfactual_touch_only"]["15"]["touch_observed"] is True
    assert row["counterfactual_touch_only"]["15"]["does_not_extend_live_timeout"] is True


def test_small_size_sensitivity_recomputes_queue_capacity() -> None:
    base = {
        "scope": {"station_id": "KLAX", "market_day": "2026-08-28"},
        "limit_price": Decimal("0.50"),
        "rule_provenance": {"status": "VALID_ARCHIVED", "min_order_size": Decimal("5")},
        "book_path": {"touch_observed": True},
        "tape_path": {"status": "OK"},
        "queue_path": {"modelled_available_shares": Decimal("15")},
        "conservative_trade_through_path": {"modelled_available_shares": Decimal("15")},
    }
    result = _small_size_sensitivity([base])
    assert result["5"]["queue_upper_bound"]["order_numerator"] == 1
    assert result["10"]["queue_upper_bound"]["order_numerator"] == 0


def test_lifecycle_labels_coverage_flicker_separately_from_true_instability() -> None:
    collector = QuietLifecycleCollector(orders=[])
    snapshot = BookSnapshot(
        timestamp=BASE,
        event_id="event-1",
        market_id="event-1:Yes",
        token_id="token-1",
        bids=((Decimal("0.39"), Decimal("10")),),
        asks=((Decimal("0.41"), Decimal("10")),),
        station_id="KLAX",
        market_day="2026-08-28",
    )
    enter = RegimeTransition(
        timestamp=BASE,
        previous_state=RegimeState.DIGESTION,
        state=RegimeState.QUIET,
        reason="quiet_window_stable",
        scope=("KLAX", "2026-08-28"),
    )
    leave = RegimeTransition(
        timestamp=BASE + timedelta(minutes=1),
        previous_state=RegimeState.QUIET,
        state=RegimeState.DIGESTION,
        reason="stability_lost",
        scope=("KLAX", "2026-08-28"),
        coverage_reasons=("UNKNOWN_CROSS_BUCKET_SYNC",),
    )
    collector.observe(ThresholdTier.NEUTRAL, snapshot, None, enter)  # type: ignore[arg-type]
    collector.observe(ThresholdTier.NEUTRAL, snapshot, None, leave)  # type: ignore[arg-type]
    assert collector.summary()["end_kind_counts"] == {"coverage_flicker": 1}


def test_episode_book_observations_count_token_native_paired_frames() -> None:
    episode = {
        "token_id": "token-1",
        "quiet_start_at": BASE,
        "quiet_end_at": BASE + timedelta(minutes=2),
    }

    def pairs() -> object:
        return iter(
            [
                {
                    "observed_at": BASE,
                    "yes": {"asset_id": "token-1"},
                    "no": {"asset_id": "other"},
                },
                {
                    "observed_at": BASE + timedelta(minutes=1),
                    "yes": {"asset_id": "other"},
                    "no": {"asset_id": "token-1"},
                },
                {
                    "observed_at": BASE + timedelta(minutes=3),
                    "yes": {"asset_id": "token-1"},
                    "no": {"asset_id": "other"},
                },
            ]
        )

    summary = annotate_episode_book_observations([episode], pairs)
    assert episode["book_observation_count"] == 2
    assert summary["book_observations_per_episode"]["p50"] == 2.0


def test_archived_gamma_metadata_restores_pre_order_rule_provenance(tmp_path) -> None:
    root = tmp_path / "data" / "raw"
    books = root / "polymarket_book_checkpoints" / "2026-08-28"
    gamma = root / "polymarket_gamma_event" / "2026-08-28"
    books.mkdir(parents=True)
    gamma.mkdir(parents=True)
    token = "token-1"
    (books / "events.jsonl").write_text(
        json.dumps(
            {
                "received_at": (BASE - timedelta(minutes=1)).isoformat(),
                "asset_id": token,
                "market_slug": "event-1:Yes",
                "book_complete": True,
                "bids": [{"price": "0.39", "size": "10"}],
                "asks": [{"price": "0.41", "size": "10"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (gamma / "events.jsonl").write_text(
        json.dumps(
            {
                "fetched_at": (BASE - timedelta(minutes=2)).isoformat(),
                "payload": {
                    "markets": [
                        {
                            "slug": "event-1:Yes",
                            "clobTokenIds": json.dumps([token]),
                            "orderPriceMinTickSize": "0.01",
                            "orderMinSize": "5",
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    quotes, _events, archive = collect_archived_order_quotes(
        [_order()], [books / "events.jsonl"], cutoff=BASE + timedelta(minutes=5)
    )
    rules = _rule_at(quotes[token], BASE)
    assert rules.valid is True
    assert rules.min_order_size == Decimal("5")
    assert rules.min_order_size_source == "gamma_event_archived_market_metadata"
    assert archive["gamma_rule_record_count"] == 1


def test_cross_bucket_sensitivity_separates_missing_buckets_from_age() -> None:
    def pairs() -> object:
        return iter(
            [
                {
                    "event_slug": "event-1",
                    "market_slug": "event-1/a",
                    "observed_at": BASE,
                    "yes": {
                        "bids": [{"price": "0.39", "size": "10"}],
                        "asks": [{"price": "0.41", "size": "10"}],
                    },
                },
                {
                    "event_slug": "event-1",
                    "market_slug": "event-1/b",
                    "observed_at": BASE + timedelta(seconds=30),
                    "yes": {
                        "bids": [{"price": "0.59", "size": "10"}],
                        "asks": [{"price": "0.61", "size": "10"}],
                    },
                },
            ]
        )

    result = cross_bucket_coverage_sensitivity(pairs)
    one_minute = result["1"]
    assert one_minute["missing_bucket_checkpoint_count"] == 1
    assert one_minute["known_count"] == 1
    assert one_minute["mass_interval_width"]["p50"] == 0.03


def test_report_distinguishes_order_and_cluster_wilson_bounds(tmp_path) -> None:
    result = {
        "forensic_version": "test",
        "source_analysis_cutoff": BASE,
        "order_count": 85,
        "execution_enabled": False,
        "primary_reason_counts": {},
        "mechanical_reason_counts": {},
        "upper_bounds": {
            "TOUCH_UPPER_BOUND": {
                "order_numerator": 0,
                "order_denominator": 85,
                "order_rate": 0.0,
                "order_wilson_95": (0.0, 0.0432),
                "station_day_numerator": 0,
                "station_day_denominator": 7,
                "station_day_rate": 0.0,
                "station_day_wilson_95": (0.0, 0.3543),
            }
        },
        "quiet_lifecycle": {},
        "cross_bucket_coverage_sensitivity": {},
        "orders": [],
    }
    output = tmp_path / "forensics.md"
    render_quiet_order_forensics_report(result, output)
    report = output.read_text(encoding="utf-8")
    assert "0/85 (0.0)" in report
    assert "0/7 (0.0)" in report
