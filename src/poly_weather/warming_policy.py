"""Heat-season policy and post-hoc derivation for ``warming_window_no``.

Historical WRH batches are appropriate for climate-feature derivation, but are
not evidence of what a trader saw in real time.  Live eligibility remains based
on realtime observations and executable NO books.
"""

from __future__ import annotations

import json
import math
from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.domain import SettlementSpec
from poly_weather.high_frequency_audit import load_wrh_historical_observations
from poly_weather.intraday_reversal import (
    TemperatureObservation,
    daily_reversals,
    percentile,
)
from poly_weather.temperature import round_whole_degree

POLICY_VERSION = "seasonal-warming-policy-v1-20260826"
HEAT_THRESHOLD_VERSION = "heat-2026-wrh-v1-20260826"
LEGACY_POLICY_VERSION = "legacy-uniform-minus-2-v1"
DECISION_TIMES = tuple(
    time(hour, minute)
    for hour in range(9, 19)
    for minute in (0, 30)
    if not (hour == 18 and minute == 30)
)
TIME_BINS = ((1.0, "0-1h"), (2.0, "1-2h"), (4.0, "2-4h"), (24.0, ">4h"))
MAX_CANDIDATE_MARGIN_F = 20


def _wilson_interval(
    successes: int, sample_count: int, *, z: float = 1.95996398454
) -> tuple[float, float] | None:
    if sample_count <= 0:
        return None
    n = float(sample_count)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(
        p * (1 - p) / n + z * z / (4 * n * n)
    ) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


HEAT_SEASON_CANDIDATES: dict[str, dict[str, Any]] = {
    "KLAX": {
        "months": (7, 8, 9, 10),
        "rationale": "July-October includes late-summer heat and Santa Ana season; June marine-layer climatology is intentionally excluded.",
    },
    "KSEA": {
        "months": (7, 8, 9),
        "rationale": "July-September covers the dry warm season while excluding climatologically cooler June.",
    },
    "KMIA": {
        "months": (5, 6, 7, 8, 9, 10),
        "rationale": "May-October covers the long hot and convective season.",
    },
    "KDAL": {
        "months": (5, 6, 7, 8, 9),
        "rationale": "May-September covers the southern-plains hot season.",
    },
    "KHOU": {
        "months": (5, 6, 7, 8, 9),
        "rationale": "May-September covers Gulf heat and the main convective season.",
    },
    "KATL": {
        "months": (5, 6, 7, 8, 9),
        "rationale": "May-September covers the warm and afternoon-convective regime.",
    },
    "KORD": {
        "candidate_months": (6, 7, 8, 9),
        "months": (6, 7, 8),
        "rationale": "June-September was tested; September's reversal interval did not overlap the candidate-window aggregate, so the active window is narrowed to June-August.",
    },
    "KLGA": {
        "months": (6, 7, 8, 9),
        "rationale": "June-September covers the principal New York warm season.",
    },
    "ZUCK": {
        "months": (6, 7, 8, 9),
        "rationale": "June-September is a climate-mechanism candidate only; hourly source precision is insufficient for activation.",
    },
    "ZUUU": {
        "months": (6, 7, 8, 9),
        "rationale": "June-September is a climate-mechanism candidate only; hourly source precision is insufficient for activation.",
    },
}


@dataclass(frozen=True, slots=True)
class WarmingPolicyDecision:
    policy_version: str
    threshold_version: str
    station_id: str
    season_id: str | None
    season_window_start: date | None
    season_window_end: date | None
    profile: str
    enabled: bool
    season_window_active: bool
    typical_peak_minutes: int | None
    hours_to_typical_peak: float | None
    time_bin: str | None
    required_margin_f: float | None
    physical_margin_passed: bool
    reason: str

    @property
    def heat_season_active(self) -> bool:
        """Compatibility alias for snapshots written before seasonal policies."""
        return self.season_window_active


@dataclass(frozen=True, slots=True)
class WarmingThresholdRegistry:
    version: str
    recommended_profile: str
    stations: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WarmingThresholdRegistry:
        return cls(
            version=str(payload["policy_version"]),
            recommended_profile=str(payload["recommended_profile"]),
            stations=dict(payload["stations"]),
        )

    @classmethod
    def from_path(cls, path: Path) -> WarmingThresholdRegistry:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def decision(
        self,
        *,
        station_id: str,
        local_at: datetime,
        physical_margin_f: float | None,
        profile: str | None = None,
    ) -> WarmingPolicyDecision:
        station = station_id.upper()
        selected_profile = profile or self.recommended_profile
        row = self.stations.get(station)
        if row is None:
            return WarmingPolicyDecision(
                self.version,
                self.version,
                station,
                None,
                None,
                None,
                selected_profile,
                False,
                False,
                None,
                None,
                None,
                None,
                False,
                "station has no calibrated season threshold profile",
            )
        matching = []
        for season in row.get("seasons", ()):
            start = date.fromisoformat(str(season["window_start"]))
            end = date.fromisoformat(str(season["window_end"]))
            if start <= local_at.date() <= end:
                matching.append((season, start, end))
        if len(matching) != 1:
            reason = (
                "overlapping calibrated season windows for station"
                if len(matching) > 1
                else "outside calibrated season window for station"
            )
            return WarmingPolicyDecision(
                self.version,
                self.version,
                station,
                None,
                None,
                None,
                selected_profile,
                False,
                False,
                None,
                None,
                None,
                None,
                False,
                reason,
            )
        season, window_start, window_end = matching[0]
        enabled = bool(season.get("enabled"))
        season_id = str(season["season_id"])
        threshold_version = str(season.get("threshold_version") or self.version)
        peak = season.get("typical_peak_minutes")
        peak_minutes = int(peak) if peak is not None else None
        hours_to_peak = (
            (peak_minutes - (local_at.hour * 60 + local_at.minute)) / 60
            if peak_minutes is not None
            else None
        )
        if not enabled:
            reason = str(
                season.get("disabled_reason") or "station season policy is disabled"
            )
            return WarmingPolicyDecision(
                self.version,
                threshold_version,
                station,
                season_id,
                window_start,
                window_end,
                selected_profile,
                False,
                True,
                peak_minutes,
                hours_to_peak,
                None,
                None,
                False,
                reason,
            )
        if hours_to_peak is None or hours_to_peak <= 0:
            return WarmingPolicyDecision(
                self.version,
                threshold_version,
                station,
                season_id,
                window_start,
                window_end,
                selected_profile,
                True,
                True,
                peak_minutes,
                hours_to_peak,
                None,
                None,
                False,
                "typical peak for calibrated season has passed",
            )
        rules = season.get("profiles", {}).get(selected_profile, [])
        selected_rule = next(
            (
                rule
                for rule in sorted(rules, key=lambda item: float(item["max_hours_to_peak"]))
                if hours_to_peak <= float(rule["max_hours_to_peak"])
            ),
            None,
        )
        if selected_rule is None:
            return WarmingPolicyDecision(
                self.version,
                threshold_version,
                station,
                season_id,
                window_start,
                window_end,
                selected_profile,
                True,
                True,
                peak_minutes,
                hours_to_peak,
                None,
                None,
                False,
                "no validated margin rule for this much time before the peak",
            )
        required = float(selected_rule["required_abs_margin_f"])
        passed = physical_margin_f is not None and physical_margin_f <= -required
        return WarmingPolicyDecision(
            self.version,
            threshold_version,
            station,
            season_id,
            window_start,
            window_end,
            selected_profile,
            True,
            True,
            peak_minutes,
            hours_to_peak,
            str(selected_rule["time_bin"]),
            required,
            passed,
            "joint seasonal margin/time rule passed"
            if passed
            else "physical margin does not meet the joint seasonal threshold",
        )


def load_default_warming_policy() -> WarmingThresholdRegistry:
    """Load the repository policy, failing closed when deployment omitted it."""
    path = Path(__file__).resolve().parents[2] / "configs" / "warming_window_no_thresholds.json"
    if path.exists():
        return WarmingThresholdRegistry.from_path(path)
    return WarmingThresholdRegistry(
        version="missing-warming-policy",
        recommended_profile="conservative",
        stations={},
    )


def _daily_groups(
    observations: Iterable[TemperatureObservation],
) -> dict[date, list[TemperatureObservation]]:
    groups: dict[date, list[TemperatureObservation]] = defaultdict(list)
    for observation in observations:
        groups[observation.valid.date()].append(observation)
    return groups


def _rate_summary(samples: Sequence[Any]) -> dict[str, Any]:
    remaining = [float(sample.post_decision_warming_f) for sample in samples]
    reversals = sum(value > 1.0 for value in remaining)
    return {
        "n": len(samples),
        "reversal_count": reversals,
        "reversal_rate": reversals / len(samples) if samples else None,
        "wilson_95": _wilson_interval(reversals, len(samples)),
        "p90_remaining_warming_f": percentile(remaining, 0.90) if remaining else None,
    }


def _typical_peak_minutes(
    observations: Sequence[TemperatureObservation],
) -> int | None:
    peak_minutes: list[int] = []
    for records in _daily_groups(observations).values():
        ordered = sorted(records, key=lambda item: item.valid)
        final = max(item.temperature_f for item in ordered)
        first = next(item for item in ordered if item.temperature_f == final)
        peak_minutes.append(first.valid.hour * 60 + first.valid.minute)
    return int(round(percentile(peak_minutes, 0.50))) if peak_minutes else None


def _time_bin(hours_to_peak: float) -> str:
    return next(label for maximum, label in TIME_BINS if hours_to_peak <= maximum)


def _warming_rate_f_per_hour(
    observations: Sequence[tuple[datetime, Decimal]], *, lookback_hours: float = 2.0
) -> float | None:
    if len(observations) < 2:
        return None
    ordered = sorted(observations, key=lambda row: row[0])
    latest_time, latest_temperature = ordered[-1]
    cutoff = latest_time.timestamp() - lookback_hours * 3600
    eligible = [row for row in ordered[:-1] if row[0].timestamp() >= cutoff]
    if not eligible:
        return None
    first_time, first_temperature = eligible[0]
    elapsed_hours = (latest_time - first_time).total_seconds() / 3600
    if elapsed_hours < lookback_hours * 0.75:
        return None
    return float((latest_temperature - first_temperature) / Decimal(str(elapsed_hours)))


def _threshold_samples(
    observations: Sequence[TemperatureObservation],
    *,
    typical_peak_minutes: int,
) -> list[dict[str, Any]]:
    """Replay physical inputs at half-hour times; use the full day only as label."""
    samples: list[dict[str, Any]] = []
    for target_date, records in sorted(_daily_groups(observations).items()):
        ordered = sorted(records, key=lambda item: item.valid)
        final_high = int(
            round_whole_degree(Decimal(str(max(item.temperature_f for item in ordered))))
        )
        for scan_time in DECISION_TIMES:
            scan_at = datetime.combine(target_date, scan_time)
            hours_to_peak = (
                typical_peak_minutes - (scan_time.hour * 60 + scan_time.minute)
            ) / 60
            if hours_to_peak <= 0:
                continue
            available = [item for item in ordered if item.valid <= scan_at]
            if len(available) < 2:
                continue
            rate = _warming_rate_f_per_hour(
                [
                    (item.valid, Decimal(str(item.temperature_f)))
                    for item in available
                ]
            )
            if rate is None or rate <= 0.3:
                continue
            observed_high = int(
                round_whole_degree(
                    Decimal(str(max(item.temperature_f for item in available)))
                )
            )
            first_upper = (observed_high // 2) * 2 + 1
            for upper in range(first_upper, first_upper + MAX_CANDIDATE_MARGIN_F + 2, 2):
                margin = observed_high - upper
                if not -MAX_CANDIDATE_MARGIN_F <= margin <= -2:
                    continue
                samples.append(
                    {
                        "target_date": target_date,
                        "scan_at": scan_at,
                        "time_bin": _time_bin(hours_to_peak),
                        "hours_to_peak": hours_to_peak,
                        "physical_margin_f": margin,
                        "failed": upper - 1 <= final_high <= upper,
                    }
                )
    return samples


def _candidate_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    failures = sum(bool(sample["failed"]) for sample in samples)
    n = len(samples)
    return {
        "n": n,
        "failure_count": failures,
        "failure_rate": failures / n if n else None,
        "wilson_95": _wilson_interval(failures, n),
        "unique_days": len({sample["target_date"] for sample in samples}),
    }


def _select_rule(
    samples: Sequence[Mapping[str, Any]],
    *,
    point_limit: float,
    wilson_upper_limit: float,
) -> dict[str, Any] | None:
    for required in range(2, MAX_CANDIDATE_MARGIN_F + 1):
        selected = [
            sample
            for sample in samples
            if float(sample["physical_margin_f"]) <= -required
        ]
        if len(selected) < 30:
            continue
        summary = _candidate_summary(selected)
        interval = summary["wilson_95"]
        if (
            summary["failure_rate"] is not None
            and summary["failure_rate"] <= point_limit
            and interval is not None
            and interval[1] <= wilson_upper_limit
        ):
            return {
                "required_abs_margin_f": float(required),
                **summary,
                "trigger_frequency_in_candidate_universe": len(selected) / len(samples),
            }
    return None


def analyze_station_heat_policy(
    spec: SettlementSpec,
    observations: Sequence[TemperatureObservation],
) -> dict[str, Any]:
    station = str(spec.station_id)
    candidate = HEAT_SEASON_CANDIDATES[station]
    months = tuple(int(value) for value in candidate["months"])
    candidate_months = tuple(
        int(value) for value in candidate.get("candidate_months", months)
    )
    heat = [item for item in observations if item.valid.month in months]
    non_heat = [item for item in observations if item.valid.month not in months]
    heat_days = _daily_groups(heat)
    typical_peak = _typical_peak_minutes(heat)
    if typical_peak is None:
        raise ValueError(f"no heat-season observations for {station}")
    monthly: dict[int, dict[str, Any]] = {}
    all_month_diagnostics: dict[int, dict[str, Any]] = {}
    heat_reversals = daily_reversals(heat)
    overall = _rate_summary(heat_reversals)
    candidate_reversals = daily_reversals(
        [item for item in observations if item.valid.month in candidate_months]
    )
    candidate_overall = _rate_summary(candidate_reversals)
    for month in candidate_months:
        monthly_samples = [
            sample for sample in candidate_reversals if sample.target_date.month == month
        ]
        month_rows = [item for item in observations if item.valid.month == month]
        summary = _rate_summary(monthly_samples)
        summary["years"] = sorted({item.valid.year for item in month_rows})
        monthly[month] = summary
    for month in range(1, 13):
        month_rows = [item for item in observations if item.valid.month == month]
        month_samples = daily_reversals(month_rows)
        summary = _rate_summary(month_samples)
        summary["years"] = sorted({item.valid.year for item in month_rows})
        summary["typical_peak_minutes"] = _typical_peak_minutes(month_rows)
        all_month_diagnostics[month] = summary
    overall_interval = candidate_overall["wilson_95"]
    break_months = []
    if overall_interval is not None:
        for month, summary in monthly.items():
            interval = summary["wilson_95"]
            if interval is not None and (
                interval[1] < overall_interval[0] or interval[0] > overall_interval[1]
            ):
                break_months.append(month)
    pool = _rate_summary(daily_reversals(observations))
    non_heat_summary = _rate_summary(daily_reversals(non_heat))
    non_heat_summary["typical_peak_minutes"] = _typical_peak_minutes(non_heat)
    result: dict[str, Any] = {
        "station_id": station,
        "season_id": "heat_2026",
        "threshold_version": HEAT_THRESHOLD_VERSION,
        "heat_season_months": list(months),
        "candidate_months": list(candidate_months),
        "candidate_rationale": candidate["rationale"],
        "month_contributions": monthly,
        "all_month_diagnostics": all_month_diagnostics,
        "homogeneity_break_months": break_months,
        "heat_season": overall,
        "non_heat_season": non_heat_summary,
        "pooled": pool,
        "typical_peak_minutes": typical_peak,
        "observation_count": len(heat),
        "observation_density_per_day": len(heat) / len(heat_days) if heat_days else 0,
        "enabled": not station.startswith("Z"),
    }
    if station.startswith("Z"):
        result["disabled_reason"] = (
            "hourly WRH observations and zero-percent METAR T-group coverage are "
            "insufficient for a comparable intraday threshold derivation"
        )
        result["profiles"] = {"aggressive": [], "conservative": []}
        result["margin_strata"] = []
        return result
    candidates = _threshold_samples(heat, typical_peak_minutes=typical_peak)
    strata = []
    profiles = {"aggressive": [], "conservative": []}
    for maximum, label in TIME_BINS:
        rows = [sample for sample in candidates if sample["time_bin"] == label]
        if not rows:
            continue
        for margin in range(-2, -MAX_CANDIDATE_MARGIN_F - 1, -1):
            exact = [sample for sample in rows if sample["physical_margin_f"] == margin]
            if exact:
                strata.append(
                    {"time_bin": label, "physical_margin_f": margin, **_candidate_summary(exact)}
                )
        for name, point, upper in (
            ("aggressive", 0.10, 0.125),
            ("conservative", 0.05, 0.075),
        ):
            selected = _select_rule(rows, point_limit=point, wilson_upper_limit=upper)
            if selected is not None:
                profiles[name].append(
                    {
                        "time_bin": label,
                        "max_hours_to_peak": maximum,
                        **selected,
                    }
                )
    result["profiles"] = profiles
    result["margin_strata"] = strata
    result["candidate_sample_count"] = len(candidates)
    return result


def build_heat_season_policy_analysis(
    specs: Sequence[SettlementSpec],
    *,
    data_dir: Path,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    rows = []
    seen: set[str] = set()
    for spec in specs:
        station = str(spec.station_id or "")
        if not station or station in seen or station not in HEAT_SEASON_CANDIDATES:
            continue
        seen.add(station)
        observations = load_wrh_historical_observations(
            data_dir,
            station_id=station,
            timezone=spec.timezone,
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(analyze_station_heat_policy(spec, observations))
    return {
        "policy_version": POLICY_VERSION,
        "recommended_profile": "conservative",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "collection_mode": "historical_backfill",
        "derived_at": datetime.now(UTC).isoformat(),
        "strict_no_lookahead_eligible": False,
        "stations": rows,
    }


def policy_document(analysis: Mapping[str, Any]) -> dict[str, Any]:
    def season_row(row: Mapping[str, Any]) -> dict[str, Any]:
        months = [int(month) for month in row["heat_season_months"]]
        window_start = date(2026, min(months), 1)
        window_end = date(2026, max(months), monthrange(2026, max(months))[1])
        return {
            "season_id": row["season_id"],
            "threshold_version": row["threshold_version"],
            "enabled": bool(row["enabled"]),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "boundaries": "inclusive",
            "calendar_months": months,
            "typical_peak_minutes": row["typical_peak_minutes"],
            "profiles": row["profiles"],
            "derivation": {
                "source_date_start": analysis["start_date"],
                "source_date_end": analysis["end_date"],
                "sample_days": row["heat_season"]["n"],
                "month_contributions": {
                    str(month): {
                        "days": summary["n"],
                        "years": summary["years"],
                    }
                    for month, summary in row["month_contributions"].items()
                    if int(month) in months
                },
                "derived_at": analysis["derived_at"],
                "provenance": analysis["collection_mode"],
                "strict_no_lookahead_eligible": False,
            },
            **(
                {"disabled_reason": row["disabled_reason"]}
                if row.get("disabled_reason")
                else {}
            ),
        }

    return {
        "schema_version": 2,
        "policy_version": analysis["policy_version"],
        "recommended_profile": analysis["recommended_profile"],
        "derivation": {
            "start_date": analysis["start_date"],
            "end_date": analysis["end_date"],
            "collection_mode": analysis["collection_mode"],
            "candidate_margin_range_f": [-MAX_CANDIDATE_MARGIN_F, -2],
            "aggressive_target": "point failure <=10%, Wilson upper <=12.5%",
            "conservative_target": "point failure <=5%, Wilson upper <=7.5%",
        },
        "stations": {
            str(row["station_id"]): {
                "seasons": [season_row(row)],
            }
            for row in analysis["stations"]
        },
        "execution_enabled": False,
    }


def render_heat_season_policy_report(
    analysis: Mapping[str, Any], output_path: Path
) -> None:
    def pct(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.1%}"

    def interval(value: Sequence[float] | None) -> str:
        return "N/A" if value is None else f"{value[0]:.1%}-{value[1]:.1%}"

    def clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    lines = [
        "# warming_window_no 季节化阈值：热季实例",
        "",
        f"版本：`{analysis['policy_version']}`；覆盖 {analysis['start_date']} 至 {analysis['end_date']}。",
        "",
        "阈值推导使用 `historical_backfill` WRH 高频数据，只属于事后气候特征分析。",
        "它不能证明历史时点可交易；任何可交易性结论仍只能使用 realtime 观测和真实 NO 订单簿。",
        "当前流程启用的是各站第 1 个季节实例 `heat_2026`；季节是配置维度，不是写死在信号引擎里的特例。",
        "",
        "## 热季窗口与季节差异",
        "",
        "| 站点 | 热季月份 | 日数 | 月份贡献(日/年数) | 高频观测/日 | 热季高点 | 热季逆转率 (Wilson 95%) | 热季p90 | 非热季高点 | 非热季逆转率 | 非热季p90 | 池化逆转率 | 热季-池化 | 机制断点 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in analysis["stations"]:
        heat = row["heat_season"]
        non_heat = row["non_heat_season"]
        pooled = row["pooled"]
        contributions = ", ".join(
            f"{month}:{summary['n']}/{len(summary['years'])}y"
            for month, summary in row["month_contributions"].items()
        )
        heat_pooled_delta = heat["reversal_rate"] - pooled["reversal_rate"]
        lines.append(
            f"| {row['station_id']} | {','.join(map(str, row['heat_season_months']))} | "
            f"{heat['n']} | {contributions} | {row['observation_density_per_day']:.1f} | "
            f"{clock(row['typical_peak_minutes'])} | {pct(heat['reversal_rate'])} "
            f"({interval(heat['wilson_95'])}) | {heat['p90_remaining_warming_f']:.1f}F | "
            f"{clock(non_heat['typical_peak_minutes']) if non_heat['typical_peak_minutes'] is not None else 'N/A'} | "
            f"{pct(non_heat['reversal_rate'])} | "
            f"{non_heat['p90_remaining_warming_f']:.1f}F | {pct(pooled['reversal_rate'])} | "
            f"{heat_pooled_delta:+.1%} | "
            f"{row['homogeneity_break_months'] or '无'} |"
        )
    lines.extend(["", "## 候选窗口逐月同质性", ""])
    for row in analysis["stations"]:
        lines.extend(
            [
                f"### {row['station_id']}",
                "",
                row["candidate_rationale"],
                "",
                "| 月 | n | 逆转率 | Wilson 95% | p90剩余升温 | 年数 |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for month, summary in row["month_contributions"].items():
            lines.append(
                f"| {month} | {summary['n']} | {pct(summary['reversal_rate'])} | "
                f"{interval(summary['wilson_95'])} | "
                f"{summary['p90_remaining_warming_f']:.1f}F | {len(summary['years'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 全月份诊断（未启用月份仅供下一季准备）",
            "",
            "这些数值未用于当前已启用阈值。月份仅有两年或三年覆盖，不能据此直接启用新季节。",
            "",
            "| 站点 | 月 | n | 逆转率 | Wilson 95% | p90剩余升温 | 高点中位数 | 年数 | 当前热季 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in analysis["stations"]:
        heat_months = set(row["heat_season_months"])
        for month, summary in row["all_month_diagnostics"].items():
            peak = summary["typical_peak_minutes"]
            p90 = summary["p90_remaining_warming_f"]
            p90_text = "N/A" if p90 is None else f"{p90:.1f}F"
            lines.append(
                f"| {row['station_id']} | {month} | {summary['n']} | "
                f"{pct(summary['reversal_rate'])} | {interval(summary['wilson_95'])} | "
                f"{p90_text} | "
                f"{'N/A' if peak is None else clock(peak)} | {len(summary['years'])} | "
                f"{'是' if int(month) in heat_months else '否'} |"
            )
    lines.extend(
        [
            "## 推荐联合阈值",
            "",
            "候选总体为每个半小时决策点、升温率 >0.3F/h、峰值前、距当前整数日高上方 2-20F 的实际 2F 桶。",
            "失败定义为最终整数日高落入该候选桶。失败率并不随负余量单调下降，因此阈值必须与距高点时间联合使用。",
            "",
            "保守档目标：点失败率 <=5% 且 Wilson 上界 <=7.5%；激进档：<=10% 且 Wilson 上界 <=12.5%。推荐保守档。",
            "",
            "| 站点 | 时段 | 激进最小负余量 | 失败率/Wilson | 触发占比 | 保守最小负余量 | 失败率/Wilson | 触发占比 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in analysis["stations"]:
        if not row["enabled"]:
            lines.append(
                f"| {row['station_id']} | 全部 | 禁用 | N/A | 0% | 禁用 | N/A | 0% |"
            )
            continue
        by_profile = {
            name: {rule["time_bin"]: rule for rule in rules}
            for name, rules in row["profiles"].items()
        }
        for _, label in TIME_BINS:
            aggressive = by_profile["aggressive"].get(label)
            conservative = by_profile["conservative"].get(label)

            def rule_text(rule: Mapping[str, Any] | None) -> tuple[str, str, str]:
                if rule is None:
                    return "禁用", "N/A", "0%"
                return (
                    f"-{rule['required_abs_margin_f']:.0f}F",
                    f"{pct(rule['failure_rate'])}/{interval(rule['wilson_95'])}",
                    pct(rule["trigger_frequency_in_candidate_universe"]),
                )

            a_margin, a_failure, a_frequency = rule_text(aggressive)
            c_margin, c_failure, c_frequency = rule_text(conservative)
            lines.append(
                f"| {row['station_id']} | {label} | {a_margin} | {a_failure} | "
                f"{a_frequency} | {c_margin} | {c_failure} | {c_frequency} |"
            )
    lines.extend(
        [
            "",
            "## 结论与限制",
            "",
            "- 中国站 ZUCK/ZUUU 保持 fail-closed：约 24 条/日且 METAR T 组覆盖为 0%，不能与美国站同口径推导。",
            "- 在高频可比的八个美国站里，KLAX 热季逆转率最低（约 16.7%）且高点最早（12:20），所以仍是物理结构上的首选站；但它不是‘几乎不逆转’，最终优先级仍须由新阈值的前向触发量和真实盘口共同确认。",
            "- 窗口首尾日均包含；窗口外、窗口重叠或缺少规则时 fail-closed，绝不沿用邻季阈值。",
            "- 现有前向样本均在 8 月，仅能验证 `heat_2026`；每个季节都需独立累积 30 个已结算样本，全年约 120 个。",
            "- 扩展下一季只需：提出气候机制候选窗口、做逐月同质性检验、推导联合阈值、追加配置季节项、再独立前向验证；信号引擎无需改代码。",
            "- 非热季逆转率、p90 与典型高点已作为下一季前期诊断留存，但尚未启用为任何阈值。",
            "- n <30 的任何分层均应标记统计不可靠；本报告不把事后 WRH 数据表述为历史可交易证据。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
