# P4 producer → consumer audit, 2026-09-08

Scope: local code and temporary fixtures only. No production archives were scanned.
This table is an engineering scope map, not economic qualification. Formal authority:
[CURRENT_CONCLUSIONS](../CURRENT_CONCLUSIONS.md#paper-v1-formal-status).

| Producer → converter → consumer | Clock / legacy policy | Identity / closure / quality | Durable and report authority / unresolved scope |
| --- | --- | --- | --- |
| Data API adapter → collector journal → `load_event_trade_tapes` | complete-client response plus post-fact-fsync bound; row available only; legacy missing receipt retained historical-only | exact Decimal participant/token/condition/tx/time/side identity; no page closure; journal membership verified read-only for v1 receipt rows | journal fact+witness authority; tape/cursor/audit derived. Legacy corrupt files still skipped by historical loader: explicit unresolved research coverage risk |
| Same tape → `_public_trade_events_from_file` → v2 follower | no fetched_at/mtime admission fallback; row receipt before source rejected; new journal claims verified | stable transaction needed; source-to-available entire excluded quality interval checked | event IDs enter v2 ledger; mtime only change-detection optimization. Initial-scan and downtime tape recovery are not Paper-equivalent; Q06 pending for v2 |
| WS archive + API tape → `match_ws_trade` / `build_shadow_trade_events` | max WS receipt and API availability; missing/incoherent receipt rejected | exact native token/side/p/q/time/market; siblings ambiguous; disputed hash cannot API-bypass; whole source-to-validation interval checked | MATCHED_COMPLETE means row match only, never group closure; unknown upstream quality on WS rejected |
| Converter → `ShadowOrderEngine.process_trade/process_trades` | caller-supplied receipt/order clocks | diagnostic queue kernel, no trusted closure producer; local order does not establish exchange order | v2 order ledger diagnostic only; queue fills are model estimates. Not an official-score or production-completeness authority |
| Converter → Paper continuous follower → public processor | receipt-gated conversion, durable pending metadata restored | public queue admission unconditionally disabled; durable UNKNOWN group facts, alias observations and late invalidations | Paper ledger owns account/effects/consumption; source cursor is derived. `accepted_queue_trade_rows=0`; no positive public fills |
| Private Paper ordered kernel → account/strategy ledger | test-controlled clocks only | only isolated MODEL_KERNEL tests may call; production static callsite prohibition | partial/full queue and accounting tests do not establish ingress capability; previous economic effects are not rewritten by late evidence |
| CLI converter → QUIET store/replay → diagnostic result | conversion now uses shared interval filter; receipt-aware information clock exists | source closure unsupported; comprehensive vintage/future-append audit not performed this round | diagnostic state/store/report, not Paper ledger; Q04 pending, no re-run of formal QUIET reports |
| CLI converter → complement replay → diagnostic pair ledger/report | shared receipt conversion; separate token-native two legs | closure unsupported; no planned-price locked edge; no new positive claim | separate strategy ledger; no migration or production rerun. Global recovery conformance pending |
| Converter → optional Nautilus conformance | same offline candidate inputs, explicit sandbox path | native limitations retained; no exchange closure certificate | `official_score=false`, challenger-only, execution false; local fee formula compared separately from native model limitations |
| Historical tape → public analytics | source-time historical analysis may retain UNKNOWN_RECEIPT; not admissible forward trading evidence | trades never substitute resting ask/bid/depth | reports only; missing/corrupt tape coverage remains N/A/unknown risk, not empty-success qualification |

## Focused fixes and limits

Q01: `jsonl_archive_paths` previously returned both plain/gzip, allowing repeated evidence.
Now equal bytes are streamed/hashed with before/after metadata checks, plain selected;
conflicting/changing pairs fail closed, neither file removed. Test covers equal/conflict.
Earlier compression-only saved-cursor uncertainty is superseded for the tested
Paper schema-2 follower and shared reader: actual plain/both/gzip-only, restart,
cursor crash and future-prefix equivalence now have isolated tests. Signal JsonlTail
uses the same bound prefix. This is not global cursor migration: old nonzero
positions without a prefix hash block. Retention deployment remains deferred.

Q03: prior endpoint-only quality checks admitted an interval with an interior incident.
Shared WS/API match and both API-only converters now use existing inclusive interval
overlap helper. Tests put an incident strictly between source and receipt, leaving
both endpoints clean. Journal-backed tape claims are reverified by both loaders without
writing journal/audit. Receiptless legacy data is not promoted. Full quality provenance
of historical API-only rows remains unknown; their diagnostic models are not certified.

Q05: fixed local Weather rate 0.05, token-native p, ROUND_HALF_UP to 0.00001 USDC,
maker zero, taker q×rate×p×(1−p). Shared wrappers tested at four native prices with
Decimal. Paper books the kernel's explicit fee; optional Nautilus native fee model is
not silently assumed identical. No online parameter/fee update or universal aggregation
rounding equivalence claim.

Q06: new receipt tests cover fact/witness/tape/cursor/audit crash/replay, checksums,
anchors, conflict preservation, and same first receipt. Existing Paper model partial
fill/consumption atomicity tests remain MODEL_KERNEL. Full v2/QUIET/complement
multi-file recovery equivalence and every OS power-loss edge remain unverified.

Q02 (legacy weather mode), Q04 (full vintage OOS), Q07 (large-scale performance),
Q08 (complete license/historical revision audit) remain pending. The optional dependency
pin is preserved, not reinstalled. This is NOT project-wide closure.
