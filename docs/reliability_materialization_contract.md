# E01 bounded materialization contract (2026-09-09; before implementation)

Local durable member completeness is not upstream group closure. Paper remains disabled.

A materialization binds one event and a journal prefix anchor, exact first-receipt
deduplicated members plus immutable baseline legacy rows, count and the complete
derived payload. Store a checksummed immutable manifest in the receipt journal,
addressed by the digest of that payload, before publishing tape. Tape holds its
manifest digest. A marker-less tape in an event with journal facts/manifests cannot
be accepted as legacy. Truly unbound legacy remains unverified historical data.

First event fact binds the existing legacy baseline without upgrading receipt. Later
facts cannot amend that baseline. Exact set validation is against the declared prefix,
not the latest journal tail. Duplicates, removed fields/members, wrong event/anchor,
count or payload contradictions fail closed. A failed query has no complete-response
claim; a successful zero-member query remains distinct.

| Crash | Recovery/read rule |
| --- | --- |
| Before fact | Retry with later response time |
| Fact without witness | Readers may verify an older bounded prefix, never this fact; writer validates existing anchors/tapes before adding recovery witness |
| Witness before manifest | Missing tape may be reconstructed; existing contradictory tape is preserved and blocked |
| Manifest before tape | Manifest is immutable; old committed tape remains valid at its own prefix |
| Tape before cursor | Reconcile idempotently, then acknowledge cursor |
| Cursor before audit | Rebuild derived audit |

Readers never repair. Bounded prefix reads ignore later uncommitted tail; corruption
within the claimed prefix blocks. Writer must validate the complete existing chain,
cursor anchors and all existing in-scope tapes before any recovery mutation.
Batch readers bound their shared journal snapshot to the maximum requested tape
anchor, then independently validate each manifest/payload against its own anchor.
A damaged unclaimed tail can be ignored by readers, never by recovery writers.
The receipt fact completion bound is not the actual admission time: actual admission
occurs only after witness and materialization publication. Delayed witness recovery
uses a later bound. No infinite witness-of-witness timestamp protocol is introduced.

Joint consistent rollback of journal and all external anchors remains undetectable.
Manifest deletion/unknown old provenance fails closed; this is not authenticated
protection against an adversary who rewrites every authority consistently.
