# Paper V1 seal test matrix

## Current authority — 2026-09-09, local evidence-closure delivery (NOT SEALED)

**Public Paper ingress: accepted_queue_trade_rows=0. Positive public queue
consumption is UNSUPPORTED pending source-contract proof; NOT SEALED.**
Formal evidence/startup authority: [single source](../CURRENT_CONCLUSIONS.md#paper-v1-formal-status).
Current names, parameterized counts, source hashes and diagnostic exclusions are generated
only in [reliability_test_inventory.json](reliability_test_inventory.json). Collection is not PASS.
Each collected case has a primary evidence level; unqualified cases are UNVERIFIED,
including cases whose historical integration label has not been re-audited.

Scoped closure assertions. Results are linked to the current validation artifact;
temporary production ingress is not a deployment or positive public-fill claim.

| IDs | Actual test name | Layer and boundary | Remaining scope |
| --- | --- | --- | --- |
| EC01–EC04 | `test_ec_missing_or_contradictory_members_rejected` | PRODUCTION_INGRESS; actual tape loaders | No source group-closure claim |
| EC05 | `test_ec_cross_file_commit_recovery` | PRODUCTION_INGRESS; six publications before/after, two recoveries | OS power-loss semantics not certified |
| EC06 | `test_ec_readers_ignore_corrupt_unclaimed_tail_but_writer_blocks` | PRODUCTION_INGRESS; bounded readers vs recovery writer | Joint rollback of all authorities undetectable |
| EC07 | `test_ec_reader_cost_measurement` | Diagnostic reads/bytes/time/memory | Large-file latency unqualified |
| AR01/AR02 | `test_ar_real_follower_plain_gzip_restart_equivalence`; `test_signal_tail_plain_gzip_partial_and_prefix_replacement` | Actual follower/reader, model-prepared nonempty ledger, fixed-clock future append | Legacy nonzero cursor without hash blocked; no migration |
| WE01 | `test_we_invalid_weather_cannot_become_realtime` | Observation join/information/signal ingress | Unverified legacy sources remain unknown |
| WE02 | `test_we_signal_information_join_prefix_survives_late_high`; `test_forecast_append_preserves_actual_signal_and_information_prefix`; `test_fixed_forecast_requires_every_model_initialization` | Actual signal/information/join; nonempty fixed-vintage fixture | Real Open-Meteo missing initialization remains UNKNOWN; no all-source OOS claim |
| HE01 | `test_actual_producer_commits_two_durable_samples`; `test_signal_progress_requires_successful_state_commit`; `test_he_short_polls_accumulate_pending_work_window` | Real synchronous publication; shared status reader with fake OS evidence | No daemon deployment/power-loss/long-run fsync-latency certification |
| HE02 | `test_he_any_required_gate_unknown_blocks_readiness`; `test_he_input_ready_is_not_public_score_eligibility` | Evidence composition used by follower; signal/shadow business gates integrated | Actual rules evidence missing in a row remains false, not substituted |
| HE03 | `test_he_rejected_entry_still_expires_and_releases`; `test_he_rejected_entry_preserves_native_risk_exit_gate` | MODEL_KERNEL inventory; actual snapshot/sweep rejection, fresh vs stale native quote | No public ingress fill claim |

Current execution results and explicit limits: [closure report](reliability_evidence_closure_report_20260909.md),
[machine validation](reliability_evidence_closure_validation_20260909.json).

| Current assertion | Actual test name | Primary evidence level | Scope |
| --- | --- | --- | --- |
| Replayed evidence prefix retains zero fills after failed cycle | `test_replayed_evidence_prefix_is_idempotent_after_cycle_failure` | CONTAINMENT_ONLY | Replaces the old claimed successful-fill prefix proof |
| Evidence append failure prevents cursor acknowledgement | `test_evidence_append_oserror_halts_without_cursor_advance` | CONTAINMENT_ONLY | Not a currently reachable follower account-fill commit proof |
| Downtime tape is revisited, without queue effects | `test_restart_replays_downtime_public_trade_once` | CONTAINMENT_ONLY | Stable mtime/restart; zero fills, not once-filled |
| Later API match resolves matching, not group closure | `test_later_public_match_resolves_match_but_not_group_completeness` | CONTAINMENT_ONLY | Matched rows may be nonzero; accepted queue rows stay zero |
| Public group batch/file/restart invariance | `test_f02_native_follower_batch_file_switch_restart` | CONTAINMENT_ONLY | With/without sequence; zero queue consumption |
| First receipt survives duplicate materialization | `test_f03_distinct_clocks_and_duplicate_receipt` | PRODUCTION_INGRESS | Collector/row conversion; post-fact-fsync witness, not group closure |
| Pending quality does not upgrade | `test_f01_native_follower_two_polls_restart_preserves_quality` | CONTAINMENT_ONLY | Bad/missing quality remains unqualified |
| Synthetic economics is restart-idempotent | `test_restarted_economic_trade_is_consumed_once` | MODEL_KERNEL | Private kernel only; no positive production admission claim |
| Queue consumption/post-order persistence | `test_partial_fill_and_trade_consumption_are_one_durable_fact` | MODEL_KERNEL | Ordered model inputs, not wire completeness proof |
| Positive production closure | No admissible source-contract proof yet | UNSUPPORTED | No caller boolean/fixture certificate can authorize it |
| Receipt crash boundaries | `test_receipt_crash_reconciliation` | PRODUCTION_INGRESS | Fact/witness/tape/cursor/audit; temporary collector files only |
| Journal corruption | `test_receipt_corruption_preserves_bytes_and_blocks` | PRODUCTION_INGRESS | Tail/interior/checksum/rollback; no automatic truncation |
| Single health truth table | `test_health_read_status_cli_and_downstream_share_truth` | CONTAINMENT_ONLY | Fake OS providers; no resident daemon observation |
| Interior quality gap | `test_q03_quality_interval_interior_rejected_by_all_converters` | CONTAINMENT_ONLY | Both endpoints clean, interval still rejected |
| Fee math | `test_q05_fixed_weather_fee_math_and_shared_wrappers` | MODEL_KERNEL | Fixed local native-token rate; not online fee verification |

Actual execution results and limitations: [next-phase report](reliability_next_phase_report_20260908.md).

The historical sections below are **SUPERSEDED in full as current authority**.
Their old counts, short test aliases, deleted names, PASS labels and claims of
positive follower fills are retained solely as an audit trail. Do not merge
them into this current matrix. Stage 2 changed positive economics tests to
private model-kernel tests and changed native ingress assertions to zero consumption.

<!-- SUPERSEDED_HISTORY -->
## SUPERSEDED historical records — not current PASS or capability evidence

## 2026-09-08 trade evidence repair supplement (T01-T20)

This supplement supersedes the old assumption that price/size/archive sequence
prove distinct siblings. Historical results below are retained. PASS is scoped
to the named temporary fixtures, not formal Paper operation or independent review.
The new file is `tests/test_paper_trade_evidence.py` (53 cases); earlier durability
and runtime tests in `tests/test_paper_seal_blockers.py` remain mandatory.

| ID | Actual assertion / test evidence | Result |
| --- | --- | --- |
| T01 | restarted_economic_trade; same_batch_cross_source_alias_and_scientific_decimal: trailing zeroes/scientific notation leave consumption/fills unchanged | PASS |
| T02 | same_batch_cross_source_alias; bidirectional_alias_repeated_restart; real_follower_two_polls[data_api]: one economic event | PASS |
| T03 | symmetric market_ws parameters, including two-poll follower and restart: first legal fill retained, replay empty | PASS |
| T04 | sequence_conflict_is_durable_unknown: queue unchanged and UNKNOWN survives reload | PASS |
| T05 | explicit_normalized_row_ids_preserve_transaction_siblings: two caller-supplied row IDs under one hash/token produce two consumed keys and never replay | PASS, normalized input only; current public wire per-fill ID provenance remains N/A |
| T06 | indistinguishable_same_source_batch; unproven_same_transaction_siblings; mixed_batch: no summing/overwrite, durable UNKNOWN | PASS |
| T07 | bidirectional size=50, three restarts: queue/order identical and fills=0 | PASS |
| T08 | bidirectional size=101/1000: complete account, orders, reserves, fees, station cost, tranche/exit state and consumed keys equal | PASS |
| T09 | ws_quantity_conflict: public 1 / WS 1000 yields no event; follower conflict parameter rejects API fallback across polls/restart | PASS |
| T10 | each_conflicting_field plus timestamp_precision: explicit quantity/price/side/token/market/time rejection | PASS |
| T11 | mixed_batch: only tx-1 accepted, tx-2 rejected; repeated public/WS candidates not overwritten | PASS |
| T12 | timestamp_precision_and_validation_dependency_receipt, precision_loss_and_naive_time: max receipt, UTC equivalence, no submicrosecond truncation or naive timezone | PASS |
| T13 | delayed_evidence_does_not_fill_expired_order; later_public_match; follower market_ws: pending before API, resolve only after receipt, expired order remains empty | PASS |
| T14 | pending_restart_uses_complete_validator; pending_multiple_ws_and_future_receipt; follower conflict: wrong quantity/time/multiple candidates remain pending | PASS |
| T15 | crash_before_after_durable_identity_boundaries: observation/order/account-intent/account-commit before and after append, all eight recover/replay to clean economics; alias_observation_crash adds both alias boundaries | PASS |
| T16 | identity_write_oserror_does_not_commit_follower_cursor plus account-commit OSError/HALT tests: cursor byte-identical, no post-HALT economics | PASS |
| T17 | failed_cycle_with_alias_partial_prefix_matches_clean_economics: real follower failed prefix, alias arrival and restart equal clean account/order/queue/stages/consumption | PASS |
| T18 | legacy_key_halts_paper_and_v2_key_is_unchanged; plain_shadow_cursor; shared shadow runtime fixtures: legacy Paper fails closed, normal v2 key/cursor unchanged | PASS, no automatic historical migration |
| T19 | real_follower_two_polls_then_restart (3 parameters): actual checkpoint/WS/API parsers, matching, ledger and cursor; repeated invocation preserves state | PASS |
| T20 | paper_runtime_boundaries, paper_cli, nautilus_conformance plus ending read-only data checks: no execution client/default Nautilus loading, no formal Paper files | PASS within test/check scope |

T19 seeds a pre-existing isolated order and parses an additional token's real
checkpoint fixture. It does not claim that incomplete-weather fixtures authorize
a new strategy order. T05 supplies IDs through the existing normalized row-ID
contract; it does not invent a WS/Data API trade-ID field. Production hash-only
siblings remain UNKNOWN, not a measured loss or zero-fill opportunity.

Full suite remains blocked by socketpair initialization, and the old upstream
reviewed-revision gap remains open. See `paper_v1_seal_validation_status.md` for
commands, results, safety checks and qualification of the repair candidate.

## Historical seal-blocker matrix

This matrix is the review authority for the 22 Paper V1 requirements. It is
updated for `CODEX_PAPER_V1_SEAL_BLOCKERS_TASK.md`; it does not authorize a
Paper runtime start. Every listed fixture is rooted under `tmp_path` (or a
test-only temporary directory), and the tests call local production functions
directly only to exercise file-boundary recovery. They never invoke the Paper
CLI or create a formal `data/` artifact.

`PASS` means the named assertion passed in the focused Paper suite. It does
not mean that Paper has been started, that an exchange order exists, or that a
formal score is available.

Seal status as of 2026-09-08: **NOT SEALED**. The full default pytest run is
blocked by local socketpair/asyncio initialization; standalone CPython 3.12 and
3.14 reproduce the timeout. A diagnostic run excluding five async-related test
files passed 394 tests, but is not a replacement for the required complete run.
The historical reviewed revision for polymarket-tmax-lab is also unavailable
in the recovered notice/history. See `paper_v1_seal_validation_status.md`.

| # | Exact requirement | Exact test name | Production code path | Unit / integration | Restart / crash boundary | Production metadata contract | Actual assertions | PASS / FAIL |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | A continuous poll expires a resting order even with no later token frame. | `test_continuous_poll_expires_without_new_token_frame_exactly_once` | `run_paper_spread_continuous -> sweep_lifecycle -> ShadowOrderEngine.expire` | Continuous-loop integration | Poll restart after first expiry | Frozen strategy timeout is 900 seconds; isolated temp ledger/cursor only. | A 901-second-old order becomes `EXPIRED`; one durable `release_buy` exists. | PASS |
| 2 | A partially filled BUY expiry releases only its unfilled reservation. | `test_lifecycle_sweep_expires_without_later_token_snapshot_and_releases_residual` | `sweep_lifecycle -> _sync_terminal_orders -> record_account_action` | Processor integration | Timeout after queue-aware partial fill | Native token order/fill facts and Decimal reservation accounting. | One filled share remains inventory/cost; residual reservation reaches zero without releasing the filled cost. | PASS |
| 3 | A repeated lifecycle sweep cannot release the same reservation twice. | `test_continuous_poll_expires_without_new_token_frame_exactly_once` | `run_paper_spread_continuous -> _sync_terminal_orders` | Continuous-loop integration | Second complete poll after first terminal transition | Terminal transition ID is `expire:<order_id>:<timestamp>`. | The ledger has exactly one `release_buy` after two cycles; the durable order stays `EXPIRED`. | PASS |
| 4 | Closing one event cancels/releases only that portfolio's maker reservation. | `test_closed_event_releases_only_its_own_portfolio_reservation` | `sweep_lifecycle -> close_portfolio_key -> _sync_terminal_orders` | Processor integration | Closed-event lifecycle transition | Token portfolio identity is `event_id + market_id + token_id + market_day`. | Event A BUY is cancelled, Event B stays active, and only A's $20 reservation is released. | PASS |
| 5 | A closed event with a fresh, native bid may use the Paper risk-exit model. | `test_closed_event_with_contemporaneous_native_bid_risk_exits` | `sweep_lifecycle -> close_portfolio -> _risk_exit_eligible` | Processor integration | Closed-event transition | Same token, complete/healthy book, native bid, and current timestamp. | Position closes with zero stranded cost and zero remaining shares. | PASS |
| 6 | Missing native bid leaves inventory stranded/unpriced despite tempting surrogate prices. | `test_no_native_bid_ignores_all_surrogate_prices` | `close_portfolio -> _risk_exit_eligible -> _strand` | Processor integration | Close without executable bid | Fixture includes midpoint, last trade, opposite-token, `1-p`, historical, and settlement values; `bids=()`. | No SELL order is made; position is stranded, historical cost remains occupied, realized PnL remains zero. | PASS |
| 7 | Distinct station/token portfolios share one global $200 account. | `test_processor_portfolios_share_one_global_200_account` | `PaperSpreadProcessor._paper_buy -> PaperAccount.reserve_buy` | Processor integration | N/A | Frozen KLAX/KLGA entry bands and one `PaperAccount(initial_cash_usd=200)`. | Ten $20 reservations across KLAX/KLGA consume $200; the eleventh portfolio creates no order. | PASS |
| 8 | BUY, partial SELL/release, fees, and cash preserve Decimal account conservation. | `test_global_account_reserves_cash_and_conserves_value` | `PaperAccount.reserve_buy/fill_buy/release_buy/fill_sell/assert_conservation` | Account unit | N/A | Decimal-only account event schema. | Expected cash, reservation, inventory cost, fees, equity, and realized PnL are asserted exactly. | PASS |
| 9 | A SELL can never exceed same-token inventory before an order/fill is accepted. | `test_exit_stages_use_initial_share_quarters_without_oversell`; `test_account_rejects_sell_above_same_token_inventory` | `_submit_exit_if_eligible -> ShadowOrderEngine.submit_maker_exit`; `PaperAccount.fill_sell` | Processor integration + account unit | Exit-stage retry boundary | Exit share target derives from same-token durable BUY shares. | Maker exits request no more than current inventory; a direct over-inventory sell is rejected. | PASS |
| 10 | Restart reconstructs account, orders, strategy state, and capital clock from the append-only ledger. | `test_restart_preserves_account_orders_strategy_evidence_and_capital_time` | `PaperLedger.reconcile_startup -> restore_paper_account -> _restore_strategy_state` | Restart integration | Restart after orders/fills/decisions | Paper ledger envelope, strategy config hash, token portfolio keys. | Restored account, capital metrics, active orders, supervisor/quality evidence, weather state, and exit state equal pre-restart values. | PASS |
| 11 | An ambiguous/corrupt ledger state durably HALTs; uniquely implied recovery remains exactly once. | `test_discrepancy_is_durable_halt`; `test_recovery_repair_is_unique_across_restarts` | `PaperLedger.record_discrepancy/reconcile_startup` | Restart/crash integration | Persisted discrepancy and repeated recovery | Append-only transition IDs and Paper ledger envelope. | Discrepancy causes durable halt; a uniquely repairable transition is committed once across repeated restarts. | PASS |
| 12 | An exit releases current cash but never resets station-day cumulative buy cost. | `test_exit_releases_cash_without_resetting_station_day_cumulative_buy_cost` | `_record_fills -> _station_day_available` | Processor integration | N/A | `station_day_budget_mode=cumulative_buy_cost`. | Exit clears reservation/shares while cumulative daily BUY cost remains exactly $20. | PASS |
| 13 | The four frozen tranches obey their weather, price, fill, and prior-fill gates. | `test_initial_tranche_requires_lag_improving_and_no_ask_band`; `test_fourth_tranche_requires_actual_third_fill`; `test_conditional_dip_requires_two_ticks_from_latest_actual_fill` | `process_snapshot -> _weather_gate/_paper_buy` | Processor integration | N/A | Frozen strategy version, entry bands, actual token fill history, receipt-ordered weather evidence. | Each gate is asserted to reject/accept only its declared prerequisite; fourth tranche requires actual third fill. | PASS |
| 14 | One weather observation is receipt-ordered, consumed once, and rejected evidence remains unconsumed. | `test_confirmation_is_receipt_ordered_consumed_once_and_requires_prior_fill`; `test_rejected_confirmation_is_not_consumed` | `PaperWeatherEvidence.parse -> _weather_gate -> _consume_observation` | Processor/restart integration | Restart restores consumed evidence | Ordering key is `(source_timestamp, received_at, observation_id)`. | Old/rejected evidence does not consume state; eligible confirmation is accepted once after prior fill. | PASS |
| 15 | Worsening weather cancels exposure and cannot be used to replenish later. | `test_weather_worsening_cancels_and_releases_buy_reservation` | `process_snapshot -> close_portfolio` | Processor integration | Worsening event followed by otherwise eligible snapshot | Production weather metadata includes explicit worsening flag. | Initial BUY is cancelled, reservation is released, later eligible-looking snapshot creates no replacement order. | PASS |
| 16 | Stale, maintenance, unreadable, and quality-excluded data cannot open an entry or price a risk exit. | `test_stale_snapshot_cannot_open_entry`; `test_risk_exit_rejects_quality_window_between_quote_and_decision`; `test_risk_exit_interval_keeps_all_native_quote_boundaries_fail_closed` | `process_snapshot`, `_paper_buy`, `_risk_exit_eligible` | Processor integration | Quote-to-decision incident interval | `health_ok`, quality interval, native bid, age in `[0,300]`, feed/supervisor generation. | Stale entry makes no order; overlap, ended-but-overlapping incident, future quote, stale quote, and no-bid quote all reject. | PASS |
| 17 | Exit stages use actual shares, durable stage progress, residual retry, and no over-sell. | `test_four_exit_stages_conserve_shares_and_final_stage_clears_remainder`; `test_partial_exit_timeout_retries_only_stage_residual` | `_exit_stage_target -> _submit_exit_if_eligible -> _record_fills` | Processor/restart integration | Partial exit timeout/retry | Native bid/ask and same-token inventory only. | Four exits sell exactly cumulative BUY shares; retry submits only the unfilled stage residual. | PASS |
| 18 | Every frozen config field and Paper ledger identity are enforced. | `test_exact_paper_config_rejects_each_tampered_field`; `test_paper_ledger_rejects_non_paper_schema_collision` | `PaperStrategyConfig.validate`; `PaperLedger._validate_envelope` | Config/ledger unit | Restart loader boundary | Schema/version, false Boolean execution flag, strategy fields, `ledger_kind=paper_spread_v1`. | Each frozen field tamper raises; a shadow-v2-shaped false-execution row still makes the Paper ledger invalid. | PASS |
| 19 | A first continuous start tail-bootstraps and does not score old archive history. | `test_paper_continuous_first_start_tail_bootstraps_without_scoring_history` | `run_paper_spread_continuous -> ShadowCursor.bootstrap_at_tail` | Continuous-loop integration | First-start boundary | Isolated Paper cursor and local archive paths. | Status says tail bootstrap, new market rows are zero, and no active order is created. | PASS |
| 20 | Replay uses a resting order's event clock, never wall-clock time. | `test_replay_expiration_uses_resting_order_event_clock` | `replay_paper_spread -> sweep_lifecycle` | Replay integration | Historical `t+899` then `t+900` | Replay snapshots carry explicit UTC event timestamps. | A 2020 resting order remains at 899 seconds, expires exactly at 900 seconds, and status reports the replay timestamp. | PASS |
| 21 | Paper config, ledger rows, and status remain strictly read-only. | `test_paper_records_and_status_are_strictly_read_only_and_scan_clear` | Config/ledger writer/status serializer | Processor integration | N/A | `execution_enabled` is the Boolean `false` in every tested row/status. | Status and every ledger row assert `execution_enabled is False`; dependency scan is clear. | PASS |
| 22 | Windows runner construction remains supervised, isolated, and cannot use `--once`; forbidden execution capability scan remains clear. | `test_windows_paper_runner_arguments_are_supervised_and_isolated`; `test_paper_records_and_status_are_strictly_read_only_and_scan_clear` | `scripts/windows/poly-weather-daemon-runner.ps1` argument template; `execution_dependency_scan` | Static command-contract + processor integration | N/A | Frozen Paper config and isolated ledger/status/cursor paths. | Test reads the PowerShell template without launching it and asserts supervised/runtime/config/ledger/status/cursor arguments and no `--once`; scan has no forbidden modules. | PASS |

## Seal-blocker fault and evidence regressions

The 22 requirements above are supplemented by the following direct A1-A11 and
B1-B6 seal checks. These are named here so an independent reviewer can run the
same narrow evidence without inferring coverage from this table.

| Seal area | Exact test name(s) | Concrete assertion |
| --- | --- | --- |
| A1 staged cursor transaction | `test_handled_cycle_exception_does_not_commit_source_cursor`; `test_replayed_successful_prefix_is_idempotent_after_cycle_failure` | Every injected cycle stage keeps source offset at zero; a durable replayed fill remains one fill. |
| A2 canonical trade consumption | `test_partial_fill_and_trade_consumption_are_one_durable_fact`; `test_restart_cannot_consume_partial_fill_trade_twice`; `test_queue_only_trade_replay_does_not_reduce_queue_twice`; `test_canonical_trade_key_keeps_same_transaction_rows_distinct`; `test_multiple_eligible_active_orders_recovered_durably_halts` | First post-trade order append carries canonical key; replay does not change queue/fills/cost; valid sibling rows remain distinct; ambiguous active orders halt. |
| A3 persistence failure | `test_account_commit_oserror_halts_without_cursor_advance`; `test_persistence_fault_halts_current_processor`; `test_transition_abort_oserror_halts_current_processor`; `test_unwritable_halt_record_terminates_follower_fail_closed` | Intent/order/effect/commit/abort/HALT failures freeze processing, do not commit cursor, and leave a fatal/durable halt as appropriate. |
| A4 HALT freeze | `test_halted_processor_rejects_snapshot_trade_and_lifecycle_mutation` | Ledger bytes, account, order, and strategy state are byte/field identical after all normal entry points are called while halted. |
| A5 cursor-bounded weather restart | `test_restart_weather_join_uses_only_cursor_visible_prefix` | Plain JSONL and gzip watermark prefix rebuild exactly the same join metadata as a physically trimmed archive. |
| A6 exit priority | `test_profit_exit_precedes_rejected_future_tranche` | For tranche indices 1/2/3, a qualifying bid produces stage-0 SELL before a rejected future BUY tranche. |
| A7 quality interval | `test_risk_exit_rejects_quality_window_between_quote_and_decision` | A quote immediately before an incident cannot price an exit immediately after it. |
| A8 active supervisor membership | `test_inactive_supervisor_event_cannot_open_first_order`; `test_supervisor_unreadable_empty_and_active_sets_have_distinct_entry_outcomes` | Verified-empty and unreadable receive distinct rejection codes; only verified active membership opens an order. |
| A9 downtime public tape | `test_restart_replays_downtime_public_trade_once` | A stable-mtime tape fills a recovered active order once, then replays idempotently. |
| A10 pending WS evidence | `test_unmatched_ws_trade_is_durable_pending_unknown`; `test_later_public_match_resolves_unknown_and_consumes_once`; `test_unrelated_pending_ws_trade_never_changes_another_token_queue` | Pending ambiguity survives restart and blocks readiness; exactly one later match resolves/consumes it; unrelated pending does not touch target queue. |
| A11 config/ledger isolation | `test_exact_paper_config_rejects_each_tampered_field`; `test_paper_ledger_rejects_non_paper_schema_collision` | All frozen fields and envelope identity are reject-on-tamper. |
| B1-B4 availability/touch/config/sequence | `test_nautilus_touch_case_contains_real_touch_without_trade`; `test_nautilus_local_and_native_share_availability_timeline`; `test_nautilus_sequence_limitation_is_not_reported_as_match`; `test_nautilus_report_hashes_actual_active_venue_config` | Fixture trace proves actual touch, shared availability ordering, honest sequence limitation, and hash sensitivity for every active venue parameter. |
| B5-B6 trace classification/dependency | `test_nautilus_matrix_classifications_are_backed_by_trace_assertions`; `test_optional_nautilus_sandbox_fixture_and_matrix_are_isolated`; `test_optional_nautilus_dependency_python_range_and_lock_are_pinned`; `test_conformance_import_is_lazy_and_forbidden_execution_scan_is_clear` | All 18 classifications are backed by recorded traces and declared limitations; the optional challenger is lazy, Python-bounded, lock-pinned, and has no execution capability. |

The append-only Paper ledger is the recovery authority. Checkpoints, cursor
state, and the Paper-only supervisor cursor are checksummed boundary evidence;
they cannot replace ledger reconciliation.
