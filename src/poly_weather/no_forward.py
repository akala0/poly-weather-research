"""Forward-only validation for NO signals using archived executable books."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poly_weather.fees import fee_per_share


def wilson_interval(successes: int, sample_count: int, *, z: float = 1.95996398454) -> tuple[float, float] | None:
    """Return a two-sided Wilson interval, or None when there is no sample."""
    if sample_count <= 0:
        return None
    if successes < 0 or successes > sample_count:
        raise ValueError("successes must be between zero and sample_count")
    n = float(sample_count)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


class NoForwardTracker:
    """Persist first trigger books and subsequent executable-bid milestones."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.state_path = data_dir / "runtime" / "no_forward_state.json"
        self.state: dict[str, Any] = {"positions": {}}
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("positions"), dict):
                self.state = loaded
        except (OSError, json.JSONDecodeError):
            pass

    def observe(
        self,
        outputs: list[dict[str, Any]],
        configs: tuple[Any, ...],
        books: dict[str, dict[str, Any]],
        generated_at: datetime,
    ) -> None:
        config_by_slug = {config.event_slug: config for config in configs}
        changed = False
        for output in outputs:
            config = config_by_slug.get(output.get("event_slug"))
            if config is None:
                continue
            market_by_id = {market.market_id: market for market in config.markets}
            for signal in output.get("signals", []):
                market = market_by_id.get(signal.get("market_id"))
                if market is None:
                    continue
                outcome_tokens = {
                    str(outcome).casefold(): token
                    for outcome, token in zip(
                        market.outcomes, market.clob_token_ids, strict=False
                    )
                }
                no_token = outcome_tokens.get("no")
                if no_token is None:
                    continue
                key = f"{config.event_slug}|{market.market_id}"
                position = self.state["positions"].get(key)
                if signal.get("warming_window_no") and position is None:
                    entry_no_ask = signal.get("no_best_ask")
                    entry_fee = (
                        float(fee_per_share(entry_no_ask))
                        if entry_no_ask is not None
                        else None
                    )
                    position = {
                        "event_slug": config.event_slug,
                        "market_id": market.market_id,
                        "market_slug": market.slug,
                        "station_id": config.station_id,
                        "target_date": config.target_date.isoformat(),
                        "no_token_id": no_token,
                        "triggered_at": generated_at.isoformat(),
                        "entry_no_ask": entry_no_ask,
                        "entry_taker_fee_per_share": entry_fee,
                        "entry_total_cost_per_share": (
                            float(entry_no_ask) + entry_fee
                            if entry_no_ask is not None and entry_fee is not None
                            else None
                        ),
                        "liquidity_role_assumption": "taker",
                        "market_fee_category": "weather",
                        "milestones": [],
                    }
                    self.state["positions"][key] = position
                    self._append(
                        {
                            "record_type": "trigger",
                            "generated_at": generated_at.isoformat(),
                            **position,
                            "physical_margin_f": signal.get("physical_margin_f"),
                            "margin_tier": signal.get("margin_tier"),
                            "warming_rate_f_per_hour": output.get(
                                "warming_rate_f_per_hour"
                            ),
                            "hours_to_typical_peak": output.get("hours_to_typical_peak"),
                            "no_best_ask": signal.get("no_best_ask"),
                            "execution_estimates": signal.get("execution_estimates"),
                            "no_book": books.get(no_token),
                            "execution_enabled": False,
                        },
                        generated_at,
                    )
                    changed = True
                if position is None:
                    continue
                no_book = books.get(no_token) or {}
                try:
                    best_bid = float(no_book.get("best_bid"))
                except (TypeError, ValueError):
                    continue
                for threshold in (0.95, 0.99):
                    label = f"bid_reached_{threshold:.2f}"
                    if best_bid < threshold or label in position["milestones"]:
                        continue
                    position["milestones"].append(label)
                    self._append(
                        {
                            "record_type": label,
                            "generated_at": generated_at.isoformat(),
                            "event_slug": config.event_slug,
                            "market_id": market.market_id,
                            "market_slug": market.slug,
                            "station_id": config.station_id,
                            "target_date": config.target_date.isoformat(),
                            "no_token_id": no_token,
                            "triggered_at": position["triggered_at"],
                            "elapsed_minutes": (
                                generated_at
                                - datetime.fromisoformat(position["triggered_at"]).astimezone(UTC)
                            ).total_seconds()
                            / 60,
                            "no_best_bid": best_bid,
                            "no_book": no_book,
                            "execution_enabled": False,
                        },
                        generated_at,
                    )
                    changed = True
        if changed:
            self._atomic_state()

    def record_settlement(self, raw: dict[str, Any], received_at: datetime) -> None:
        """Attach a WebSocket resolution to every tracked token in that market."""
        winning = str(raw.get("winning_asset_id") or "")
        assets = {str(value) for value in raw.get("assets_ids", [])}
        if not winning or not assets:
            return
        changed = False
        for position in self.state["positions"].values():
            no_token = str(position.get("no_token_id") or "")
            if no_token not in assets or "settled_at" in position:
                continue
            no_won = no_token == winning
            entry = position.get("entry_no_ask")
            entry_fee = position.get("entry_taker_fee_per_share")
            pnl = (
                (1.0 - float(entry) if no_won else -float(entry))
                - float(entry_fee or 0)
                if entry is not None
                else None
            )
            position["settled_at"] = received_at.isoformat()
            position["no_won"] = no_won
            position["settlement_pnl_per_share"] = pnl
            self._append(
                {
                    "record_type": "settlement",
                    "generated_at": received_at.isoformat(),
                    **position,
                    "winning_asset_id": winning,
                    "execution_enabled": False,
                },
                received_at,
            )
            changed = True
        if changed:
            self._atomic_state()

    def _append(self, row: dict[str, Any], generated_at: datetime) -> None:
        path = (
            self.data_dir
            / "raw"
            / "no_forward_validation"
            / generated_at.astimezone(UTC).date().isoformat()
            / "events.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def _atomic_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)


def forward_summary(data_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted((data_dir / "raw" / "no_forward_validation").glob("*/events.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    triggers = [row for row in rows if row.get("record_type") == "trigger"]
    settlements = [row for row in rows if row.get("record_type") == "settlement"]
    wins = sum(bool(row.get("no_won")) for row in settlements)
    net_pnls = []
    for row in settlements:
        entry = row.get("entry_no_ask")
        if entry is None:
            continue
        entry_fee = row.get("entry_taker_fee_per_share")
        if entry_fee is None:
            # Compatibility for forward rows captured before the official fee
            # curve was implemented. The own NO entry price is preserved.
            entry_fee = float(fee_per_share(entry))
        net_pnls.append(
            (1.0 - float(entry) if row.get("no_won") else -float(entry))
            - float(entry_fee)
        )
    return {
        "trigger_count": len(triggers),
        "settled_count": len(settlements),
        "wins": wins,
        "win_rate": wins / len(settlements) if settlements else None,
        "wilson_95": wilson_interval(wins, len(settlements)),
        "mean_net_pnl_per_share": (
            sum(net_pnls) / len(net_pnls)
            if net_pnls
            else None
        ),
        "bid_095_count": sum(row.get("record_type") == "bid_reached_0.95" for row in rows),
        "bid_099_count": sum(row.get("record_type") == "bid_reached_0.99" for row in rows),
        "statistically_reliable": len(settlements) >= 30,
        "execution_enabled": False,
    }


def render_forward_report(summary: dict[str, Any], output_path: Path) -> None:
    interval = summary["wilson_95"]
    interval_text = (
        f"{interval[0]:.1%}–{interval[1]:.1%}" if interval is not None else "N/A"
    )
    win_rate = summary["win_rate"]
    win_rate_text = f"{win_rate:.1%}" if win_rate is not None else "N/A"
    reliability = "可用" if summary["statistically_reliable"] else "统计不可靠（n < 30）"
    mean_pnl = summary["mean_net_pnl_per_share"]
    mean_pnl_text = "N/A" if mean_pnl is None else f"{mean_pnl:.4f}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(
            [
                "# NO 侧前向验证增量报告",
                "",
                "全部成本与退出只取 NO token 自身真实订单簿，不使用 `1−YES` 或 `p` 代理。",
                "",
                "| 触发数 | 已结算 | 胜数 | 真实胜率 | Wilson 95% | bid≥0.95 | bid≥0.99 | 可靠性 |",
                "|---:|---:|---:|---:|---:|---:|---:|---|",
                f"| {summary['trigger_count']} | {summary['settled_count']} | {summary['wins']} | "
                f"{win_rate_text} | {interval_text} | {summary['bid_095_count']} | "
                f"{summary['bid_099_count']} | {reliability} |",
                "",
                "按 NO 自身入场 ask 扣除 Weather taker 手续费后的平均 "
                f"P&L/份：{mean_pnl_text}。",
                "",
                "`execution_enabled=false`；本报告仅做前向观测，不会产生订单。",
                "",
            ]
        ),
        encoding="utf-8",
    )
