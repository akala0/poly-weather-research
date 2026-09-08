# Paper V1 trade evidence repair contract (2026-09-08)

Status: repair candidate with focused regression evidence; NOT SEALED.
The T01-T20 matrix and validation-status document record results and limitations.

## Pre-fix evidence

`tests/test_paper_trade_evidence.py`: 3 failed in 0.33s before production changes.
Decimal-equivalent replay and API-to-WS sequence enrichment both produced an
additional fill after restart. Public size 1 incorrectly authorized WS size 1000.

## Identity

Separate economics (token, side, exact UTC instant, exact finite Decimal price
and quantity), source observations (transaction hash, source, receipt, sequence),
and ordering. Decimal trailing zeroes are representation only. Sequence is not
an economic identifier. No float, tick rounding or timestamp tolerance is allowed.

The current PublicTrade and MarketWsTrade models expose transaction hashes, not
verified per-fill IDs. A hash is a transaction group, not proof of a unique fill.
Unresolvable siblings or conflicting observations must remain UNKNOWN. Never sum
ambiguous quantities or infer a log index from archive sequence.

The existing normalized `TradeEvent.from_mapping.id` input is kept distinct from
an optional transaction hash. Tests with explicitly supplied independent row IDs
prove that this contract preserves siblings. No new upstream WS/API per-fill-ID
field is presumed: current hash-only wire siblings still fail closed.

Paper identity versioning must not migrate or rewrite the resident v2 ledger.
Old Paper evidence without enough fields for a unique reconstruction must HALT.
Consumed identity and the post-consumption queue/order must share one durable
record; account recovery uses the existing intent/commit reconciliation. An
observation or alias alone is not a consumption commit. Crashes before an order
record retry; after an order record reconcile the uniquely implied account effect
or HALT. An unresolved identity can never be promoted merely by a restart.

## Cross-source matching

Validate each WS observation separately. A unique complete match requires hash,
token, side, exact price/quantity/time and compatible market identity. Missing
receipt, malformed numbers, multiple candidate matches, precision mismatch and
conflicting fields are UNKNOWN. Neither first/last-row selection nor batch-wide
permission is allowed. Availability is the maximum of both dependency receipts.
Historical rows with no receipt cannot validate strict forward evidence.

Quarantine disputed transaction groups on both WS and API paths. An unrelated
API trade can supplement independently. Pending resolution must reuse the same
matcher and preserve validation availability; it cannot resurrect terminal orders.

## Shared callers requiring regression coverage

`market_trade_tape`: CLI replay/report paths at build_shadow_trade_events calls;
shadow_runtime offline replay and continuous follower; Paper continuous follower;
market trade tape unit tests. `canonical_trade_event_key`: ShadowOrderEngine and
PaperSpreadProcessor. V2 persistence remains isolated and must be regression tested.

## Acceptance

The requested T01-T20 matrix, parser/follower two-poll tests, durability fault
tests and bounded full suite remain mandatory. A small passing reproducer is not
evidence that the entire repair is complete. No Paper CLI, formal data writes,
daemon operations, dependency changes, commit or push are authorized.
