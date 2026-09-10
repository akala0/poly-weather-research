# P3 fixed health truth table — 2026-09-08

Declared before code/tests. Future skew tolerance is **5 seconds**, inclusive;
default freshness maximum 300 seconds, overridden by existing per-daemon limits.
Age remains signed; timestamps without timezone or malformed primary heartbeat
are invalid. A present invalid heartbeat never falls back to another field.

| Evidence, evaluated in priority order | effective_state | ready |
| --- | --- | --- |
| File missing | not_started | false |
| File unreadable/corrupt, no usable backup | unreadable | false |
| PID confirmed dead, any reported state including reconnecting | stopped | false |
| PID missing / invalid for live report | stale | false |
| PID reused / command mismatch | ownership_mismatch | false |
| Integrity not verified, including last-good fallback | stale for live report | false |
| Heartbeat missing / invalid | stale for live report | false |
| Heartbeat age < -5 seconds | clock_skew for live report | false |
| Heartbeat age > configured threshold | stale for live report | false |
| OS access or command ownership unknown | unknown for live report | false |
| Dependency unhealthy or unknown | degraded for otherwise live report | false |
| Explicit progress stalled | stalled | false |
| All required liveness evidence valid, dependencies healthy/not_required | reported lifecycle state | only running/connected/healthy |

Integrity, reported lifecycle, PID/ownership, heartbeat, dependency, progress,
effective lifecycle, and named reasons are separate fields. A single status snapshot
cannot establish cursor advancement: progress defaults to UNKNOWN, never advancing.
`health_ready` means normalized operational liveness/dependency gate, not proof of
useful collection progress, economic admissibility, or successful automatic recovery.
Business consumers retain independent completeness/quality/freshness gates.

Known status filenames map to fixed expected daemon commands. Windows command
ownership reuses the existing CIM approach, bounded and restricted to the one PID;
OpenProcess remains the liveness primitive. Access failure is unknown, not alive.
Process creation after the recorded heartbeat means reused. No existing daemon
will be queried or operated during these tests; providers are fakes or test self.

CLI chain dependencies (2026-09-09 correction): market, supervisor and raw weather
collection are independent roots; weather collection must not wait for market;
signal needs market/supervisor/weather; shadow needs those plus signal. Shared
chain normalization supplies readiness to runner and signal/shadow gates. Paper
requires normalized supervisor health, not checksum alone. Historical/replay model
tests must explicitly provide health evidence or remain blocked; fixtures do not
qualify production ingress. No runner script is executed in this task.

## Business evidence candidate (2026-09-09; not deployed/certified)

`business_sample` is a normalized producer-evidence contract, not an assumed
existing wire field. It requires sampled_at, run_id, generation, committed_positions
and continuity. An idle claim additionally needs explicit pending_work=false,
verified connection and advancing completed_checks. No first/absent sample is
made healthy by a consumer-generated timestamp. Candidate market/weather writers
publish positions only after flushing and fsyncing all touched raw handles.
Supervisor progress advances only after atomic active-set publication; its business
generation binds membership/evidence, not the status-write counter. Signal advances
only after atomic signal-state publication of the input positions. Each producer
persists its previous actual sample. Existing deployed producers have not been
restarted or migrated: absence of this evidence still means UNKNOWN.

Read-chain comparison retains the actual earlier unchanged sample across short
polls; otherwise a 5-second polling cadence would never reach a 300-second stalled
window. Per-service windows reuse declared health maximum ages. Operational health
and business readiness remain separate. Paper combines business progress with
market/supervisor/weather health, active membership, weather/rules/season/quality,
archive continuity and account/ledger evidence. Missing evidence blocks BUY, not
lifecycle release or independently qualified native risk exits. Signal and v2 shadow
now require upstream business readiness; v2 retains the baseline across polls.
Actual synchronous producer publication and failures are covered by
tests/test_producer_progress_closure.py. This is temporary ingress certification,
not resident health or Windows power-loss certification. Local fsync continuity
does not prove upstream WebSocket completeness. Raw collection remains independent
of downstream strategy readiness; polling frequency was not reduced.
