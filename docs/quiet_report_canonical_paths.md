# QUIET 报告 canonical 路径

`QUIET` 的默认入口是 v2；v1 仅保留作不可变的历史证据，不能再用来判断 QUIET 是否存在、maker 是否可成交，或作为 live 状态来源。

| 用途 | Canonical 路径 | 口径 |
|---|---|---|
| 四态 / external-information clock | `data/information_reaction_report_v2.md` | receipt-gated HARD/SOFT/NO_OP/INVALID |
| QUIET maker 主报告 | `data/quiet_window_strategy_v2_report.md` | token-native v2 replay |
| QUIET v2 机器结果 | `data/quiet_window_strategy_v2_analysis.json` | 当前 token-native v2 重放；不是旧 85 单 cohort 的替身 |
| 预声明主 size grid | `data/quiet_window_v2_size_grid.json` | 不是参数搜索 |
| 零成交逐单审计 | `data/quiet_order_forensics_report.md` / `data/quiet_order_forensics.json` | 保存的 85 单 cohort；TOUCH / QUEUE / CONSERVATIVE 上界 |
| 前向状态 ledger | `data/raw/quiet_window_state/quiet_state_v1.jsonl` | 状态证据 ledger v1；首次只 tail-bootstrap，随后只追加新证据 |

旧文件 `data/quiet_window_strategy_report.md`、`data/quiet_window_strategy_analysis.json`、`data/quiet_window_size_grid.json`、`data/information_reaction_report.md` 和 `data/information_clock_analysis.json` 是 v1 只读证据，均已 superseded；不删除、不覆盖、不作为默认入口。

复跑当前 v2，或审计显式保存的固定 cohort：

```powershell
.venv\Scripts\python.exe -m poly_weather analyze-quiet-window --forensics
.venv\Scripts\python.exe -m poly_weather analyze-quiet-window --forensics --forensics-source <保存的_JSON>
```

每次 `--forensics` 都会先将指定输入复制为同目录的
`*_forensic_source_<UTC cutoff>.json`，再刷新 canonical v2 产物；若要审计另一固定 cohort，必须显式传入
`--forensics-source <保存的 JSON>`。无该参数时审计的是当前 v2，不能把修复 provenance 后的当前 v2 orders 与报告中的 85 单 cohort 混为独立样本。

前向状态日志必须单独运行，避免把历史固定-vintage forensic 当作前向证据：

```powershell
.venv\Scripts\python.exe -m poly_weather analyze-quiet-window --forward-state-log --no-size-grid
```

两条命令都只读本地公开归档，硬编码 `execution_enabled=false`，不会启动、停止或修改 lead-lag、complement 或 market/weather/signal 三个常驻流程。`--forensics` 与 `--forward-state-log` 故意不可合并：前者固定历史分析截止点，后者只在当前归档尾部建立前向边界。前向状态检查如需调度，频率上限为每 5 分钟一次；这不降低或改变市场订单簿的实时采集频率。
