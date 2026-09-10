# E02 archive position v2 (2026-09-09, local candidate)

Callsites inspected: Paper and shadow continuous followers use ShadowCursor and
incremental reads; Paper weather/trade restart builders use committed prefixes.
CLI research, depth_calibration, liquidity and information_clock enumerate archives
without incremental positions; weather_market_join uses plain/gzip text readers.

Logical identity removes only the `.gz` suffix. Representation identity retains
actual path and file identity. New per-source position schema 2 stores **decompressed
byte offset**, line count and SHA256 of confirmed bytes. Offsets never mean gzip
compressed bytes. Newline-terminated UTF-8 records only; blank lines count, incomplete
last lines wait. Malformed complete rows block without advancing position.

Plain→gzip switch is accepted only if the confirmed prefix hash/length matches.
Legacy nonzero cursor without prefix proof is blocked, even in its existing
representation: UNKNOWN_ARCHIVE_PREFIX. Missing/truncated/conflicting prefixes block;
no reset-to-zero or repeated tail bootstrap. Cursor publication remains caller-owned.
Paper restart reconstructs exactly that prefix, never later weather.

No retention deletion is authorized. Rehashing confirmed prefixes can be expensive
for large archives; only bounded fixtures qualify here, not production-scale latency.
