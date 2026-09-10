# Durable receipt contract — v1, 2026-09-08

Written before P1 implementation. This is local candidate code, not deployment or
Paper admission authority. `execution_enabled=false`; group completeness remains
unsupported. No network or formal-data migration is authorized.

## Clocks and authority

`source_timestamp` is the upstream assertion; `request_started_at` is local request
start; `response_received_at` is the clock after the complete client result (not an
individual HTTP page). None proves durability. `first_seen_at` preserves this first
response observation only after its receipt fact is durable. `last_seen_at` is not
used for admission; this version preserves rows unchanged on duplicates.

An immutable numbered fact is written with the existing fsync/atomic-replace writer.
Only AFTER that writer returns is `receipt_committed_at` sampled. It is a conservative
observed-completion bound for the **fact**, not an invented timestamp of the last
fsync instruction. An immutable witness records that bound and the fact digest.
Consumers require both verified objects; the witness does not claim its own later
fsync happened at that bound. `decision_visible_at` / `available_at` is the maximum
of source, response and observed fact-commit clocks. The already durable fact
contains all members needed for reconstruction at that bound; the witness certifies
the bound. No decision is actually emitted until witness persistence returns.

If a crash leaves a fact without its witness, recovery verifies the fact and creates
a witness with the **later recovery clock**, never reconstructing the lost completion
clock. A complete fact+witness is a journal commit. Its
`receipt_journal_sequence` and digest identify the immutable receipt. A later crash
before tape writing restores the same first receipt and witness bound.

`file_written_at` is a pre-write materialization-attempt clock, NOT a claim that
fsync has completed and NOT an admission clock. Cursor and audit are derived state.

HTTP response in RAM followed by a crash before any durable write is unrecoverable.
Retry uses a later receipt. Request time, source time, mtime, file time, and guesses
must never restore an earlier one. Legacy rows remain historical-only/UNKNOWN_RECEIPT.

## Commit and recovery order

Complete response → capture response → immutable fact fsync → sample observed
completion → immutable witness fsync → idempotent tape → cursor → audit.

The existing collector OS locks enclose all operations. The journal is an append-only
sequence of immutable JSON objects, not a mutable latest-status file or JSONL append.
Each fact hashes its predecessor and normalized members; each witness binds its fact.
Cursor and tape hold high-water anchors. Missing/reordered/interior-corrupt objects,
checksum mismatch, or anchor rollback block collection and preserve bytes. There is
no automatic truncation protocol: even a damaged final committed object fails closed.
Unique writer `.tmp` leftovers are uncommitted and never promoted or cleaned by the
reader. Loss of the entire journal AND all external anchors cannot be detected locally.

After fact+witness but before tape: materialize from journal without another request.
After tape but before cursor: idempotent materialization then restore cursor.
After cursor but before audit: regenerate audit. Receipt conflicts with existing tape
block the object and retain old bytes; discrepancy is a separate immutable diagnostic.
Request failure records an error fact with no accepted members and no watermark advance.
It stores `failure_observed_at`, `response_complete=false`, and a null response receipt;
an exception is never relabeled as a complete HTTP response.
The current adapter does not expose page receipts or closure: page coverage and upstream
quality are explicitly UNKNOWN, including when a complete Python list was returned.

Identity uses exact Decimal values, token, condition, source time, side, transaction and
public participant identity. Decimal formatting and local sequence are not new economic
identities. This does not assert exchange-global uniqueness or group completeness.

## Platform limit

The writer checks short writes and fsyncs file contents. Windows directory-entry power-loss
durability is not certified; atomic replace is not a universal power-loss guarantee.
Process-crash tests cannot qualify power failure. F04 remains partially unverified and
Paper remains NOT SEALED regardless of these tests.
