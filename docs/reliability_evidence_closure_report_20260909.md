# Evidence closure — local delivery, 2026-09-09

## Final local candidate (supersedes checkpoint status, preserves its history)

### Independent review update — 2026-09-09

The same 220-file candidate passed an independent full pytest run:
**741 passed in 34.44s, exit 0, no excluded files or skips**. The reviewer used
a 180-second outer bound with verbose per-test output, duration reporting and a
20-second faulthandler threshold; the process completed normally. Fingerprints
matched before and after the run. Evidence is preserved in
`reliability_independent_full_validation_20260909.json`.

This supersedes the full-suite blocker below for this tested candidate/environment.
The earlier 60-second timeout remains historical evidence and does not prove the
current socket root cause. No system repair was performed or inferred.
Documentation was subsequently updated for the user's explicitly authorized GitHub
publication; the old 220-file fingerprint remains the pre-documentation candidate,
not a new hash claim for the edited reports. Publication does not authorize Paper
startup, daemon deployment or formal data migration. Remaining source, compatibility,
performance and audit limits below still apply; PROJECT NOT SEALED.

**PROJECT NOT SEALED; DO NOT START PAPER; formal N=0, PnL=N/A.**
This delivers scoped local remediation, not unconditional acceptance or deployment.
Machine evidence: `reliability_evidence_closure_validation_20260909.json`;
new candidate: `reliability_evidence_closure_fingerprint_20260909.json`.
Old fingerprints and earlier RED/checkpoint artifacts are not overwritten.

### E01 — member integrity and cross-file recovery

Exact anchored materialization members are checked by both real readers; empty,
partial deletion, downgrade, duplicates and scope contradictions cannot become a
successful empty tape. Cross-object tests cover before/after fact, witness,
manifest, tape, cursor and audit publication, two recoveries, and no reader writes.
Requested older prefixes survive an unclaimed corrupt tail; full writer recovery
still blocks corruption. Existing corrupt tape is checked before witness recovery.
The original EC01 12-failure RED output was not preserved; only its earlier narrative
exists. This gap is explicit, not replaced by a fabricated reconstructed log.

Final two-scale measurement (actual fixture reads, not production certification):

| Reader | Events | Reads | Bytes | Seconds | Peak traced bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| historical | 12 | 72 | 123476 | 0.035839 | 358464 |
| historical | 24 | 144 | 247292 | 0.086132 | 552982 |
| incremental | 12 | 72 | 123476 | 0.030112 | 330975 |
| incremental | 24 | 144 | 247292 | 0.061154 | 527560 |

### E02 — content-bound archive recovery

Six real isolated follower cases compare plain/both/gzip-only, before/after cursor
crashes, two restarts and future additions. Actual input/weather identities,
decompressed offsets/hashes, join state, nonempty orders/queue/account are compared.
Nonempty inventory is MODEL_KERNEL preparation, never public fill evidence.
Signal JsonlTail now shares prefix binding and rejects replacement rather than
silently resetting offsets; partial UTF-8 JSON lines wait for completion.
Old nonzero cursor without a prefix hash remains UNKNOWN_ARCHIVE_PREFIX. No automatic
legacy migration, formal archive rewrite, retention execution or daemon restart.

### E03 — weather eligibility and past-decision invariance

Shared mode/clock/precision/finite-value qualification applies at observation join,
information and signal ingress. Explicit corrupt temperature cannot fall back;
nested invalid WRH values cannot partially mutate signal state. Late QC previously
changed the old signal high; its RED is preserved in reliability_signal_prefix_red_20260909.json.
Signal now chooses receipt-visible weather/revisions. Actual signal/information
tests preserve a nonempty fixed-vintage prefix after a future forecast append.
Every model requires explicit initialization and valid declared lead_days >=1.
Current normalized Open-Meteo forecasts lack that initialization: UNKNOWN remains
correct. A valid synthetic vintage is consumer-contract evidence, not proof the
real source supplies vintage. Complete historical multi-source OOS is not claimed.

### E04 — business progress and entry-independent lifecycle

Real market/weather sinks publish a frontier after all touched raw files fsync;
supervisor publishes progress after atomic active-set commit; signal publishes
only after atomic state commit. Tests inject cross-file fsync and publication
failures, retain the prior frontier, and reject heartbeat-only progress. Two actual
producer samples are persisted. Shared status comparison retains earlier baselines
across short polls, detects stalls/resets and checks sample freshness/integrity.
Signal and shadow require upstream business progress, while raw collection remains
an independent root. Paper combines operational, progress, active-set, weather,
rules, season, quality, continuity and account evidence. Missing row-level rule
evidence remains false; it is not replaced by a checksum or supervisor heartbeat.
Input readiness and score eligibility are separate: unsupported public groups
cannot score even when all other gates are satisfied. Rejected entries still expire
and release reserved funds; qualified fresh native risk exits remain available,
while stale quotes strand inventory rather than invent an executable price.

### Final validation and limits

- Collected **741**; targeted **366 passed in 32.28s**.
- Diagnostic **706 passed in 42.12s**, excludes 35 nodes in fees, market_supervisor,
  wrh_backfill, stream_daemons and polymarket_status (exact files in inventory).
- Optional Nautilus **10 passed in 0.81s**, challenger-only, not public fill proof.
- Ruff passed; uv lock --check --offline passed (32 packages); Git diff check passed.
- Full default: one attempt, **60-second outer timeout**, wrapper-reported 124,
  owned test tree cleanup succeeded. No per-test faulthandler stack was captured.
  See reliability_evidence_closure_full_pytest_20260909.json. This is not PASS and
  does not establish the old socketpair root cause on this candidate. Next evidence
  needed: an independently authorized longer bounded/profiled full run, not a
  speculative Python/proxy repair. No system/network change was attempted.
- Python 3.14.3, pytest 8.4.2, Ruff 0.16.4, uv 0.12.5; HEAD unchanged at
  35ccb4530f6ec031d4b590e9ef688a49e5e60112. No commit/push or formal data edits.

Remaining boundaries are named, not silently ticked off: missing original EC01 RED
raw log; no public group completeness; unknown real forecast vintage; no old-cursor
migration; no Windows power-loss/live deployment validation. Large archive prefix
rehashing, retained signal weather-version history and added raw fsync cost still
need production-scale duration/memory/latency measurement before any deployment.
All-strategy multi-file recovery, all-source OOS, rounding aggregation and historical
revision/license provenance remain the task's explicitly deferred Q05/Q06/Q08 scope.
These are not reasons to fabricate progress, mutate live data, or start Paper.

## Historical checkpoints (not current completion claims)

**NOT SEALED — DO NOT START PAPER — formal N=0; PnL=N/A.**

This is a partial implementation checkpoint, not completion of E01–E04 or the
15-scenario acceptance plan. Earlier fingerprints remain historical, unchanged.

## Subsequent checkpoint (supersedes the implementation status below, not its measurements)

User independently reproduced the earlier 140 targeted tests (5.98s). Subsequent
changes are a different dirty-tree candidate; the old hashes do not identify it.

- EC05/EC06: `tests/test_closure_crash_matrix.py` covers before/after publication
  of fact, witness, manifest, tape, cursor and audit, with/without an existing
  tape, two repairs, immutable facts/witnesses, and corrupt-tape preflight before
  recovery witness. Readers use the maximum **requested tape anchor**, so a corrupt
  unclaimed tail does not poison an older valid prefix; writers still reject the
  whole corrupt chain. The current receipt regression set: **72 passed in 4.08s**.
- AR01/AR02 + selected WE02: six real isolated follower cases compare plain with
  both-representations/gzip-only, before/after cursor-publication crash, and two
  restarts. They compare actual input IDs, parsed weather IDs, position/hash/line,
  weather join cursor state, account, nonempty orders and portfolio state. The
  initial reserved order is explicitly MODEL_KERNEL preparation, not public fill
  evidence. Late-receipt weather/QC, later forecast and closed-event metadata
  remain outside the fixed-clock prefix. Six cases passed in 5.12s before later
  unrelated changes; see the current combined diagnostic result below.
- WE01: shared missing-mode rejection, exact integer clocks, conservative
  nanosecond availability, timezone/clock-conflict/nonfinite checks now apply to
  observation join and signal ingest; information-clock conversion emits INVALID
  without advancing its state. WRH strict reader no longer defaults missing mode.
  The initial **6 failed / 2 passed** RED output is preserved in
  `reliability_weather_red_20260909.json`. Updated qualification + signal tests:
  **37 passed in 1.34s**. Old ordinary fixtures now declare the mode actually
  serialized by WeatherStreamSink; no legacy compatibility exception was invented.
- HE01–HE03 partial: added explicit two-sample progress assessment and full
  required-evidence composition in `business_readiness.py`; Paper follower uses
  the composition before opening. Unknown progress/active-set/rules/quality/etc.
  cannot be substituted by a healthy supervisor. Unsupported group closure keeps
  scoring false. Short polls retain an earlier actual producer baseline so they
  can accumulate a stalled interval. Weather collector startup no longer depends
  on market readiness. Rejection still permits expiry/release; supervisor write
  failures retain their HALT mutation boundary. **23 progress/readiness tests passed
  in 0.41s**; the preceding lifecycle/supervisor boundary set had 60 passes.

Remaining, not accepted: complete signal decision and fixed-vintage end-to-end
future-append invariance; production provenance for business_sample and its
committed positions/idle check; consistent business-progress enforcement in
signal/shadow rather than operational health alone; full native risk-exit matrix
under readiness rejection; final default/optional validation and final fingerprint.
The current producers do not yet publish the required certified business samples:
first/missing samples remain UNKNOWN and Paper new entries stay blocked. Do not
claim full-chain business readiness is operationally certified.

**Old nonzero cursors without prefix hashes remain blocked. No automatic migration,
tail reset or restart of the old daemon chain is authorized.** The legacy Paper
reconstruction compatibility path remains separately unqualified.

The broad diagnostic run preceding the latest changes passed 657 tests in 25.07s,
excluding five socketpair-associated files **and** document inventory checks.
That earlier number is not a result for the latest candidate. The regenerated
inventory now collects 713 cases (678 in the five-file-excluded diagnostic set).
Final fingerprints are deferred, not silently updated from the earlier 191 files.

Current candidate diagnostic: **678 passed in 34.52s, exit 0**. Command:
`.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_fees.py
--ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py
--ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py --tb=short`.
This includes the document inventory tests; it is not the complete default suite.

## Implemented and directly tested

- E01: externally committed materialization manifest binds exact rows, legacy
  baseline, count, scope and journal prefix. Deleting all/part of a tape, removing
  provenance/contract/scope, duplicate members, invalid JSON and non-object tape
  are rejected by both production loaders. Readers do not repair evidence.
- Old committed tape remains readable after later journal append or injected
  witness/materialization failure. Missing derivative reconstruction preserves
  legacy rows without inventing a receipt.
- Historical and batch incremental readers share one verification cycle, then
  recheck objects before returning. A same-mtime fact corruption is rejected.
- E02: schema-2 archive positions bind logical identity, decompressed byte offset,
  newline count and SHA256 of committed prefix. Plain→both→gzip-only works across
  two cursor restarts, including Paper committed-prefix reconstruction.
- UTF-8, blank lines, incomplete final line, truncation, replacement, malformed
  complete rows, corrupt gzip, missing file, concurrent change and false line
  count reject without publishing an advanced position. Legacy nonzero positions
  without a digest block with UNKNOWN_ARCHIVE_PREFIX; no guessed migration.

Contracts: [materialization](reliability_materialization_contract.md),
[archive positions](reliability_archive_cursor_contract.md).

## Measured verification

Command: `.venv/Scripts/python.exe -m pytest tests/test_evidence_closure.py
tests/test_archive_position_closure.py tests/test_reliability_receipts.py
tests/test_reliability_storage.py tests/test_public_trade_collection.py
tests/test_shadow_runtime.py tests/test_paper_recovery.py
tests/test_paper_runtime_boundaries.py -q --tb=short`

Result: **140 passed in 9.42s**. This is targeted regression only.
Default-sandbox run initially failed at pytest temporary-directory setup; the
authorized isolated retry passed. No production runtime was started.

Additional two-scale measurement, 4 passed (29 deselected):

| Reader | Events | Journal reads | Bytes | Seconds | Peak traced bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| historical | 12 | 72 | 123476 | 0.033988 | 348004 |
| historical | 24 | 144 | 247292 | 0.066881 | 551737 |
| incremental batch | 12 | 72 | 123476 | 0.032890 | 330380 |
| incremental batch | 24 | 144 | 247292 | 0.068466 | 526737 |

These are fixture measurements, not production latency certification. Journal
record lookup still scans in memory; archive reads rehash the confirmed prefix
and hold the unconsumed tail in memory. Large archives remain unqualified.

## Remaining acceptance work

- EC01–EC04 have concrete production-reader counterexamples in
  `tests/test_evidence_closure.py`; EC05–EC06 still need the complete cross-object
  crash matrix and reader policy for malformed uncommitted tails. The earlier
  RED run was reported as 12 failures/4 passes, but its raw output is not present
  in this checkpoint: do not claim a preserved raw RED artifact.
- EC07 measured above; no unbounded performance claim. A coordinated rollback
  of journal and all external anchors is not detectable by local hashes.
- AR01–AR02 cover production reading and cursor save/load, not yet an end-to-end
  follower comparison of input IDs, weather, queue and account through every
  crash boundary. Legacy Paper reconstruction remains a separate unaudited path;
  only schema-2 reconstruction has the new prefix guarantee.
- WE01–WE02 are **not implemented/accepted**. Inspected actual producer:
  `WeatherEvent.collection_mode=REALTIME` is serialized by `WeatherStreamSink`
  via `asdict`; WRH backfill writes explicit `historical_backfill`. Missing-mode
  defaults also exist in `weather_provenance.collection_mode` and the WRH reader,
  not only `weather_market_join.parse_weather_observation`. No verified legacy
  producer exception has been established. Receipt precision, naive timezone,
  source-after-receipt and fixed-vintage future-append tests remain required.
- HE01–HE03 are **not implemented/accepted**: separate raw weather collection
  startup dependencies, two-sample business progress, full shared Paper readiness
  and risk/lifecycle behavior under rejection remain next work.
- SA01 still requires the final affected containment regression. No change was
  made to unsupported group closure or to authorize public queue consumption.

Final diagnostic/default/optional suites, offline lock verification, full new
fingerprint and complete acceptance matrix are deferred until the final candidate;
none is inferred from the 140 targeted passes. The test inventory was regenerated
as collection metadata (636 collected), not a PASS list. Separately, 4 document
consistency tests passed with a pytest cache-permission warning; changed-source
Ruff checks passed.

## Safety boundary

No Paper start, daemon/Task Scheduler operation, network probe, install, formal
data mutation, commit or push. Git `diff --check` passed; scoped `git status --short
-- data` returned no entries. This is not a content audit of ignored data.
Git ownership was handled with invocation-local `-c safe.directory=D:/poly`, not
by changing global configuration. Temporary fixture files only were removed in
explicit retention/missing-file tests.
