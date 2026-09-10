# P2 source decision: UNSUPPORTED_GROUP_COMPLETENESS

2026-09-08, local-source audit only; no external requests. Decision precedes any
positive-admission implementation. No such implementation is authorized by the evidence.

| Question | Local evidence | Decision |
| --- | --- | --- |
| API range boundaries | `adapters/polymarket_data.py:_range` partitions integer seconds with right midpoint+1; local API reference §6.2 describes this implementation | Inclusive boundaries are a local assumption, not a certified exchange snapshot |
| Pagination, limits | limit/offset 10,000; short page ends local loop; capped full windows recursively split | No fixed snapshot token or stable pagination guarantee; not closure |
| Sorting | adapter sorts timestamp/hash/asset after fetching | Local deterministic sorting, not exchange event order |
| Response receipt | collector captures complete Python result, then durable journal | Does not preserve individual page receipts; page coverage UNKNOWN |
| Late trades | no documented upstream bounded-lateness watermark in local reference | Old second can receive new siblings indefinitely; a later poll cannot seal it |
| Trade ID | adapter explicitly says no trade-row ID; canonical identity includes participant/token/condition/time/side/Decimal values | Transaction hash alone is not unique; exact-row dedupe is not full economic identity proof |
| WS sequence | local archive sequence/run; reference §6 lists no exchange-global replay/closure sequence | Local order is not group closure; reconnect creates an unknown gap |
| WS/API alias | shared validator matches token-native price/quantity/time/side/transaction with ambiguity rejection | Match corroboration only, not completeness |
| Incident/reconnect | archive status and local quality intervals | Missing scope is UNKNOWN, not normal; endpoint health does not certify intervening interval |

Polling every N seconds, a short/empty page, poll end, file end, file switch, mtime,
increasing local sequence, or a fixture/caller `group_complete=true` never certifies
closure. There is no trusted certificate producer/verifier because local evidence
cannot justify one. Certificate-only scenarios G04 are UNSUPPORTED, not passed.

Current Paper public `process_trade(s)` journals identities and rejects queue effects:
`accepted_queue_trade_rows=0`, public fills=0. The private ordered model kernel is
test-only; static callsite tests enforce no production callers. Model queue/partial/full
and atomicity tests remain MODEL_KERNEL, not production qualification. `N=0`, `PnL=N/A`.

Late sibling observations append UNKNOWN/invalidated diagnostics and preserve prior
economic bytes. Reconnect, aliases and page splits cannot upgrade qualification.
Diagnostic v2/QUIET/complement consumers require a separate P4 audit; Paper containment
does not silently qualify those old model estimates.
