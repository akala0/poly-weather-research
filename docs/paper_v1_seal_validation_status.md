# Paper V1 validation status - 2026-09-08

## Current engineering qualification — full regression passed, not sealed (2026-09-09)

Public ingress currently has **accepted_queue_trade_rows=0**: containment only,
not restored fill capability. Private model-kernel results cannot qualify it.
Formal evidence/startup authority is [CURRENT_CONCLUSIONS](../CURRENT_CONCLUSIONS.md#paper-v1-formal-status).
Current collected test names/counts/exclusions are generated solely in
[reliability_test_inventory.json](reliability_test_inventory.json), not copied from historical runs.
Actual new-stage execution outcomes belong in `reliability_remediation_status.md`;
the inventory is collection evidence, never a passing suite result.

The historical positive-follower statements, old test names and old count claims below
are **SUPERSEDED as current authority** by the current matrix and Stage 2 containment.
Past runtime/PID observations are historical snapshots, not live status.
P1 journal and scoped crash tests, P2 UNSUPPORTED decision, P3 normalized health,
and P4 scoped consumer fixes are recorded in the
[next-phase report](reliability_next_phase_report_20260908.md).
E01–E04 scoped receipt/archive/weather/business-progress evidence is now recorded in
[evidence closure delivery](reliability_evidence_closure_report_20260909.md).
Independent full regression completed: **741 passed in 34.44s, exit 0**, no excluded
files or skips; 220 candidate fingerprints matched before and after the run.
See [independent validation](reliability_independent_full_validation_20260909.json).
Earlier timeout records below are historical; the current suite blocker is cleared
for this candidate/environment, without claiming a root-cause fix to old socket failures.
Public group closure, real forecast vintage, legacy cursor recovery, production-scale
performance and deferred audits remain unresolved. Full project sealing and deployment
are not approved. DO NOT START PAPER.

<!-- SUPERSEDED_HISTORY -->
## SUPERSEDED historical validation — retained without requalification

## Trade evidence repair candidate - 2026-09-08

The two supplied counterexamples are no longer reproduced by the regression
tests. This is a repair candidate pending independent review, not startup
authorization, formal scoring or a claim that only environment work remains.
The T01-T20 supplement is in `paper_v1_test_matrix.md`; its PASS labels are
explicitly fixture-scoped. `paper_v1_trade_evidence_contract.md` records the
contract written before production changes.

### Changes and safety boundary

At entry Git already had 10 modified tracked files (344 insertions / 69 deletions)
and the existing untracked Paper implementation, tests, tasks and configuration.
All were preserved. This task changed only the relevant trade/Paper code and tests:

Ending tracked diff: 14 files, 524 insertions / 199 deletions. This includes
pre-existing work and excludes untracked Paper files; it is not a task-only diff.
New task files are the contract, exact helper module and dedicated regression
file; existing untracked Paper/runtime/tests and the two review documents were
edited in place without staging or committing.

- `market_trade_tape.py`: exact time parsing, per-observation complete matcher,
  receipt/quality gate, explicit conflict reasons, sibling preservation and
  quarantined supplementation/merge.
- `trade_evidence.py`: new exact Decimal/time helpers; no float or tolerance join.
- `paper_spread_runtime.py`: versioned economic keys and source observation journal,
  durable ambiguity, shared pending validator, max-receipt timeout enforcement,
  batch-aware follower quarantine, consumption counters and restart-equivalent
  zero cumulative-cost initialization.
- `shadow_orders.py`: optional Paper consumption key/provenance carried in the
  same post-trade order record; ordinary v2 key path retained.
- `shadow_runtime.py`: shared parser/receipt conversion and same-cycle disputed
  transaction quarantine; no resident v2 ledger/cursor/status migration.
- `adapters/polymarket_data.py`, `trade_tape_analysis.py`: preserve original time
  text, reject precision loss, retain all tape files instead of overwriting an
  event's earlier file.
- New `test_paper_trade_evidence.py`; existing market-tape fixtures now provide
  real fixture receipts. The old sibling test incorrectly treating sequence as
  a per-fill ID now explicitly requires UNKNOWN. A pending-resolution fixture's
  API condition ID was corrected to its WS market ID rather than relaxing code.

This task did not change strategy parameters, budgets, fees, exit levels,
dependencies, notices, daemon scripts, retention, credentials or system networking.
No Paper CLI (including --once), daemon control, scheduler action, commit or push.
Only temporary follower function tests wrote ledgers/status/cursors. Tracked
`data` status/diff were empty both before and after; the formal Paper filename
search returned no matches (exit 1). These Git/filename checks are not a complete
bytewise checksum inventory of ignored data.

### Before / after and recovery evidence

Before production edits, the new reproducer command returned exit 1:
`3 failed in 0.33s`. Decimal-equivalent and API-to-WS sequence replays produced
additional ShadowFill records; public size 1 authorized WS size 1000.

The final dedicated file contains 53 passing cases. The focused suite below
also passes those original three reproducers. A dedicated boundary run returned
`8 passed, 45 deselected in 0.84s` (exit 0): observation, order, account-intent
and account-commit, each before/after durable append, all printed `halted=False`
and were asserted equal to clean economic state after recovery/replay. Both
alias-observation crash boundaries preserve the prior fill. OSError is a
different case: identity/account write tests assert durable HALT, unchanged input
cursor and zero subsequent economic mutation.

Identity v2 hashes JSON-encoded exact token/side/UTC/Decimal economics and input
identity, never source or sequence. Source aliases and receipt/sequence evidence
are journaled before use; observing an alias alone does not consume queue. The
consumed key, queue shares, fill shares and provenance are atomically present in
the post-trade order record. Existing account reconciliation repairs only the
uniquely implied effect. Contradictory sequence/economics or indistinguishable
same-source observations remain durable UNKNOWN. Missing portable per-fill IDs
are never inferred from archive sequence. Explicit normalized row IDs are
separate from transaction hash and preserve independently identified siblings.

Old Paper `trade:` keys lack sufficient side/receipt/alias proof and now HALT
for audit rather than silently restarting an empty dedupe set. No old v2 key or
formal artifact is rewritten. Historical, receipt-less tape can still be read
for research but cannot validate forward WS queue evidence.

### Commands and actual results

All pytest commands used the existing `.venv\Scripts\python.exe`, no sync/install.

| Command | Actual result |
| --- | --- |
| `-m pytest -q tests/test_paper_account.py tests/test_paper_spread_runtime.py tests/test_paper_recovery.py tests/test_paper_runtime_boundaries.py tests/test_paper_cli.py tests/test_shadow_runtime.py tests/test_paper_seal_blockers.py tests/test_market_trade_tape.py tests/test_paper_trade_evidence.py -p no:cacheprovider` | 169 passed in 11.68s, exit 0 |
| `-m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider` | 10 passed in 0.72s, exit 0; no optional-install skip |
| `-m pytest -q -p no:cacheprovider --ignore=tests/test_fees.py --ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py --ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py` | 447 passed in 15.91s, exit 0; diagnostic subset, NOT full acceptance |
| `-m ruff check src tests` | All checks passed, exit 0 |
| `uv lock --check` | Resolved 32 packages in 0.90ms, exit 0; no sync |
| `git diff --check` | exit 0; line-ending warnings only |

Shared callers covered by the diagnostic subset include CLI/replay, shadow
runtime/orders, complement pair, market regimes, public collection and tape
analysis. This is test coverage of code paths, not a re-run of historical reports
against formal data. Existing v2 results have not been re-scored.

One full-suite attempt used exactly `-m pytest -q -p no:cacheprovider -o
faulthandler_timeout=15` with an outer 60-second process timeout. It reached 50
progress dots then printed:

```text
socket.py:298 accept
socket.py:633 _fallback_socketpair
asyncio/proactor_events.py:786 _make_self_pipe
asyncio/events.py:844 new_event_loop
tests/test_fees.py:59 test_public_fee_rate_lookup_path
OUTER_TIMEOUT_60_SECONDS_NO_SUITE_RESULT
```

Only this task's test PID 33560 and child 38544 were terminated, by that exact
parent PID tree. The tool reported exit 1 after timeout (wrapper requested 124).
There is no full pytest result and no second retry of the known hang. Later local
edits were checked by the focused/diagnostic tests, not by a completed full run.

### Ending runtime and remaining limitations

Read-only `-m poly_weather stream-status --data-dir D:\poly\data` returned exit 0.
Checksum integrity was verified but every reported PID was dead: market/
supervisor 23304, weather 13324, signal 6220, shadow 8344. Heartbeats remain on
2026-09-04 around 19:45 +08:00; market reports reconnecting while the other four
are stopped with `pid_not_alive`. This is not healthy collection and was not
repaired or restarted under this task's authority.

No formal Paper artifact was found: formal N=0, PnL=N/A. Temporary fills are test
fixtures, not evidence of actual profitability. Remaining limitations include:

1. Full default pytest is unverified because of the observed socketpair hang.
2. Independent review has not occurred; T05 proves the normalized input contract,
   not that current public WS/API archives expose independently verifiable
   per-fill IDs. Hash-only ambiguous siblings remain UNKNOWN conservatively.
3. Old Paper keys require an audit/rebuild outside automatic recovery; live v2
   files are unchanged and have not been migrated or re-scored. No claim is made
   that the legacy v2 identity algorithm gained Paper's new alias journal.
4. Raw arrivals are reported per follower cycle. Durable distinct source aliases,
   economic candidates, consumed events, duplicate evidence records, siblings,
   pending/conflict and consumed quantities are separate counters; a repeatedly
   re-read archive row is not a new independent economic sample.
5. The historical reviewed revision for polymarket-tmax-lab remains unavailable;
   upstream IDs, revision hashes and missing receipts were not invented.

A 轨：未封板；不要启动模拟盘。
B 轨：隔离 challenger，非正式评分权威。

## Historical validation record (retained)

Execution is disabled. Paper has not been started. This is an incomplete
validation handoff, not a technical seal or startup authorization.

## Current blockers

1. The required `uv run pytest -q` did not finish. The 2026-09-07 attempt
   stopped after 50 passing progress dots and was interrupted (exit 1);
   interruption is not a suite result. `test_public_fee_rate_lookup_path`
   reproduces the stall. Its HTTP transport is mocked.
2. On 2026-09-08, independently installed CPython 3.14.3 and 3.12.14 both
   timed out creating a local `socket.socketpair()`. The prior traceback stops
   in `socket._fallback_socketpair -> accept`, while asyncio creates its
   internal wake-up socket pair. IPv6 socketpair also timed out. An IPv4
   blocking connection to a socket created by the same process timed out at
   `connect`. Both IPv4/IPv6 loopback routes exist. These observations establish
   a local socket communication failure, but do not identify the responsible
   proxy, filtering driver, firewall policy, or Windows component.
3. `polymarket-tmax-lab` MIT text, repository, author and adapted scope were
   recovered from Git history. The recovered original notice does not record
   the upstream reviewed revision. No hash was invented. This provenance item
   remains incomplete under L1.

No system network repair, proxy change, firewall change, daemon operation,
Task Scheduler operation, or external network probe was performed. Local
socket tests only connected sockets created by their own process. No
credentials were read. No formal data files were changed by this task.

## Available validation evidence

| Command | Result | Exit code |
| --- | --- | --- |
| `uv run pytest -q tests/test_paper_account.py tests/test_paper_spread_runtime.py tests/test_paper_recovery.py tests/test_paper_runtime_boundaries.py tests/test_paper_cli.py tests/test_shadow_runtime.py tests/test_paper_seal_blockers.py` | 110 passed in 5.76s (2026-09-07) | 0 |
| `uv run --extra nautilus-eval pytest tests/test_nautilus_conformance.py -q` | 10 passed in 0.74s (2026-09-07) | 0 |
| `uv run pytest -q --ignore=tests/test_fees.py --ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py --ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py` | 394 passed in 10.78s (2026-09-08); diagnostic subset only | 0 |
| `uv run ruff check . --exclude .claude` | All checks passed (2026-09-08) | 0 |
| `uv lock --check` | Resolved 32 packages in 1ms (2026-09-08) | 0 |

There are 429 collected tests. The 394-test subset excludes five entire files
(35 tests), including synchronous tests in those files; it must not be
reported as a complete default suite. Installed optional cases ran in this
environment. A dependency-absent full default run is not newly certified.

## Read-only runtime and formal evidence checks

`uv run poly-weather stream-status` succeeded on 2026-09-08. Selected output
fields (no process operation):

| component | state | reported_state | status_integrity | status_pid_alive | heartbeat (UTC) | status_state_reason |
| --- | --- | --- | --- | --- | --- | --- |
| market | reconnecting | null | verified | false | 2026-09-04T11:45:44.670529+00:00 | null |
| supervisor | stopped | running | verified | false | 2026-09-04T11:45:29.148221+00:00 | pid_not_alive |
| weather | stopped | running | verified | false | 2026-09-04T11:45:44.878105+00:00 | pid_not_alive |
| signal | stopped | running | verified | false | 2026-09-04T11:45:45.189532+00:00 | pid_not_alive |
| shadow | stopped | running | verified | false | 2026-09-04T11:45:27.999428+00:00 | pid_not_alive |

Verified integrity is not current liveness: all five recorded PIDs are dead.
These stale status records must not be presented as healthy live collection.
An independently running WRH backfill was observed and left untouched.

`git status --short -- data` and `git diff --stat -- data` both returned empty
stdout (exit 0). An ignored/hidden-inclusive filename search under `data/`
found no Paper V1 ledger/cursor/status (rg no-match exit 1). These checks prove
Git-visible zero changes and absence of the named formal files, not a content
hash audit of every ignored archive. No formal Paper order/fill/round-trip
evidence exists; formal N=0 and PnL is N/A.

## Remaining verification path

- Diagnose the host's failed local socket connection in an authorized system
  maintenance context. Do not assume that switching Python versions fixes it:
  both installed versions failed the same minimal check.
- Require local socketpair and asyncio creation to complete normally, then run
  the original full default pytest command without ignores or monkeypatches.
- Recover the actual historical upstream reviewed revision from primary
  historical evidence; a current upstream HEAD is not a substitute.
- Repeat the final lint, lock, formal-data and read-only runtime checks and
  obtain independent review. Formal Paper startup remains separately gated.

A 轨：未封板；不要启动模拟盘。
B 轨：隔离 challenger，非正式评分权威。
