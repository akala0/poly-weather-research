"""Pure, all-or-nothing public identity admission. Never grants settlement approval."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

from poly_weather.adapters.polymarket import EventSnapshot
from poly_weather.domain import Market, SettlementSpec
from poly_weather.modeling import market_temperature_bucket, validate_bucket_partition
from poly_weather.settlement import (
    _RULE_DATE_RE,
    _SLUG_DATE_RE,
    _STATION_RE,
    _TITLE_RE,
    _station_id,
    parse_settlement_evidence,
    verify_settlement_evidence,
)


class IdentityError(ValueError):
    """No subscriptions may be created from a partially valid set."""


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class TokenBinding:
    event_id: str
    event_slug: str
    station_id: str
    target_date: str
    market_id: str
    market_slug: str
    condition_id: str
    outcome_index: int
    outcome: str
    token_id: str


@dataclass(frozen=True)
class CollectionIdentity:
    bindings: tuple[TokenBinding, ...]
    identity_sha256: str
    projection_sha256: str
    rule_results: tuple[dict[str, Any], ...]


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise IdentityError(reason)


def _text(value: Any, name: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()) and value == value.strip(),
        f"missing_or_invalid:{name}",
    )
    return value


def _identifier(value: Any, name: str) -> str:
    _require(
        type(value) in (str, int) and bool(re.fullmatch(r"[1-9][0-9]*", str(value))),
        f"invalid_identifier:{name}",
    )
    return str(value)


def _day(description: str, target: date) -> None:
    matches = list(_RULE_DATE_RE.finditer(description))
    _require(bool(matches), "observation_date_missing")
    for match in matches:
        year = match["year"] if len(match["year"]) == 4 else "20" + match["year"]
        parsed = datetime.strptime(f"{match['day']} {match['month']} {year}", "%d %b %Y").date()
        _require(parsed == target, "observation_date_conflict")


def _station(description: str, url: str, spec: SettlementSpec) -> None:
    _require(_station_id("NOAA Timeseries", url) == spec.station_id, "station_url_conflict")
    # The source must be the reviewed NOAA host/product, not an arbitrary URL with site=.
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(url)
    sites = parse_qs(parsed.query, keep_blank_values=True).get("site", [])
    _require(
        len(sites) == 1 and sites[0].upper() == spec.station_id,
        "ambiguous_station_url",
    )
    _require(
        parsed.scheme == "https"
        and parsed.hostname == "www.weather.gov"
        and parsed.path == "/wrh/timeseries"
        and not parsed.username
        and not parsed.password,
        "unsupported_identity_source",
    )
    names = list(_STATION_RE.finditer(description))
    _require(
        bool(names) and all(m["station"].casefold() == spec.station_name.casefold() for m in names),
        "station_name_conflict",
    )
    urls = re.findall(r"https://www\.weather\.gov/wrh/timeseries\?[^\s]+", description)
    _require(
        bool(urls) and all(_station_id("NOAA Timeseries", u) == spec.station_id for u in urls),
        "description_station_conflict",
    )


def identify_events(
    selections: list[tuple[dict[str, Any], SettlementSpec, date]],
    *,
    max_tokens: int,
) -> CollectionIdentity:
    """Bind supplied source assertions; does not prove cryptographic/onchain identity.

    Unlike normal discovery conversion, no malformed child can be skipped. All raw
    identities are checked before creating any dictionary keyed by tokens. Rule
    parsing is diagnostic only and cannot grant strategy admission here.
    """
    _require(bool(selections) and 0 < max_tokens <= 512, "invalid_selection_budget")
    seen = {
        k: set()
        for k in ("event", "event_slug", "station_day", "market", "slug", "condition", "token")
    }
    bindings: list[TokenBinding] = []
    rules = []
    projections = []

    def unique(kind: str, value: Any) -> None:
        _require(value not in seen[kind], f"duplicate_or_conflicting:{kind}:{value}")
        seen[kind].add(value)

    try:
        for raw, spec, target in selections:
            _require(isinstance(raw, dict), "event_not_object")
            event_id = _identifier(raw.get("id"), "event_id")
            slug = _text(raw.get("slug"), "event_slug")
            title = _text(raw.get("title"), "title")
            description = _text(raw.get("description"), "description")
            _require(raw.get("active") is True and raw.get("closed") is False, "event_not_active")
            _require(
                re.fullmatch(spec.market_slug_pattern, slug) is not None, "event_slug_conflict"
            )
            match = _SLUG_DATE_RE.search(slug)
            _require(match is not None, "slug_date_missing")
            slug_day = datetime.strptime(
                f"{match['month']} {match['day']} {match['year']}", "%B %d %Y"
            ).date()
            _require(slug_day == target, "slug_date_conflict")
            tm = _TITLE_RE.fullmatch(title)
            _require(tm is not None, "title_date_missing")
            _require(
                datetime.strptime(f"{tm['month']} {tm['day']} {target.year}", "%B %d %Y").date()
                == target,
                "title_date_conflict",
            )
            _day(description, target)
            source_url = _text(raw.get("resolutionSource"), "resolutionSource")
            _station(description, source_url, spec)
            unique("event", event_id)
            unique("event_slug", slug)
            unique("station_day", (spec.station_id, str(target)))
            children = raw.get("markets")
            _require(isinstance(children, list) and bool(children), "markets_missing")
            markets = []
            for child in children:
                _require(isinstance(child, dict), "market_not_object")
                market_id = _identifier(child.get("id"), "market_id")
                market_slug = _text(child.get("slug"), "market_slug")
                _require(market_slug.startswith(slug + "-"), "market_event_slug_conflict")
                question = _text(child.get("question"), "market_question")
                question_day = re.search(r"\bon (?P<month>[A-Za-z]+) (?P<day>\d{1,2})\?$", question)
                _require(question_day is not None, "market_question_date_missing")
                _require(
                    datetime.strptime(
                        f"{question_day['month']} {question_day['day']} {target.year}",
                        "%B %d %Y",
                    ).date()
                    == target,
                    "market_question_date_conflict",
                )
                condition = _text(child.get("conditionId"), "condition_id")
                _require(
                    re.fullmatch(r"0x[0-9a-fA-F]{64}", condition) is not None, "invalid_condition"
                )
                _require(
                    child.get("active") is True and child.get("closed") is False,
                    "market_not_active",
                )
                enriched = dict(child)
                enriched.setdefault("description", description)
                enriched.setdefault("resolutionSource", source_url)
                _day(_text(enriched.get("description"), "market_description"), target)
                _station(
                    enriched["description"],
                    _text(enriched.get("resolutionSource"), "market_source"),
                    spec,
                )
                market = Market.from_gamma(enriched)
                _require(
                    len(market.outcomes) == len(market.clob_token_ids) == 2
                    and set(market.outcomes) == {"Yes", "No"},
                    "outcome_token_alignment",
                )
                unique("market", market_id)
                unique("slug", market_slug)
                unique("condition", condition.lower())
                for index, (outcome, token) in enumerate(
                    zip(market.outcomes, market.clob_token_ids, strict=True)
                ):
                    _identifier(token, "token")
                    _require(int(token) < 2**256, "token_out_of_range")
                    unique("token", token)
                    bindings.append(
                        TokenBinding(
                            event_id,
                            slug,
                            spec.station_id,
                            str(target),
                            market_id,
                            market_slug,
                            condition.lower(),
                            index,
                            outcome,
                            token,
                        )
                    )
                    _require(len(bindings) <= max_tokens, "token_budget_exceeded")
                markets.append(market)
            validate_bucket_partition(
                tuple(market_temperature_bucket(m, expected_unit=spec.unit) for m in markets)
            )
            # Timestamp is an explicit parsing placeholder, never persisted as source receipt.
            event = EventSnapshot(
                request_url="offline://identity-validation",
                fetched_at=datetime(2000, 1, 1, tzinfo=UTC),
                event_id=event_id,
                event_slug=slug,
                title=title,
                resolution_source=source_url,
                raw_payload=raw,
                markets=tuple(markets),
            )
            try:
                evidence = parse_settlement_evidence(event, registry_spec=spec)
                verification = verify_settlement_evidence(evidence, spec)
                rules.append(
                    {
                        "event_id": event_id,
                        "evidence": evidence.model_dump(mode="json"),
                        "verification": verification.model_dump(mode="json"),
                        "strategy_admitted": False,
                    }
                )
            except Exception as exc:
                rules.append(
                    {
                        "event_id": event_id,
                        "rule_state": "UNKNOWN",
                        "parse_error_type": type(exc).__name__,
                        "strategy_admitted": False,
                    }
                )
            projections.append(raw)
    except IdentityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise IdentityError(f"identity_conversion:{type(exc).__name__}") from exc
    ordered = tuple(sorted(bindings, key=lambda b: (b.event_id, b.market_id, b.outcome_index)))
    return CollectionIdentity(
        ordered, digest([asdict(b) for b in ordered]), digest(projections), tuple(rules)
    )
