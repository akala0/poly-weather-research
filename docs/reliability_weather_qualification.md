# Weather qualification contract — local candidate 2026-09-09

Actual producer: weather_stream.WeatherEvent → WeatherStreamSink.write(asdict)
→ weather_daemon JSONL/gzip → weather_market_join → Paper. The producer writes
explicit collection_mode, run_id, sequence, provider, product, station_id,
source_timestamp_ms and received_at_ns, plus a rendered received_at. WRH backfill
writes historical_backfill. There is no established schema/version evidence to
authorize missing-mode legacy rows; directory names do not authorize them.

| Input | Strict eligibility |
| --- | --- |
| Explicit realtime, valid source/receipt/station | Subject to consumer cutoff and other quality gates |
| historical_backfill | Post-hoc only; no strict join |
| Missing/unknown mode | UNKNOWN_WEATHER_COLLECTION_MODE; no guessed migration |
| Missing receipt, naive timezone, conflicting clocks, source after receipt | Reject with named reason |
| Nonfinite temperature | Reject; do not fall back to another temperature |

Epoch source milliseconds and receipt nanoseconds use integer arithmetic. Python
datetime has microsecond resolution: receipt nanoseconds are rounded **up** to the
next microsecond (a conservative availability upper bound), never down. Preserve
the original integer in raw evidence. WeatherStreamSink's float-rendered ISO value
may differ by at most one microsecond; larger disagreement blocks, smaller uses
the later bound. Source milliseconds must be integral. No wall-clock substitution.
METAR T-group parsing and Celsius source precision are unchanged.

Explicit invalid temperature cannot fall back to another value. Rendered receipt
with unrepresentable submicrosecond precision is rejected (raw integer nanoseconds
remain supported conservatively). Nested WRH validation precedes signal mutation.

Signal decisions select receipt-visible versions, including receipt-visible WRH
revisions when computing the daily high and warming rate. Late revisions and future
fixed-vintage forecasts are tested through actual signal/information functions.
Forecast qualification requires explicit initialization for every model, no future
initialization, and any declared lead_days must be an integer >=1. Process run_id,
hourly forecast valid times and collection times never substitute initialization.
The actual normalized Open-Meteo producer currently supplies no fixed initialization:
those forecasts stay UNKNOWN/diagnostic. Positive vintage fixtures certify the
consumer contract only, not availability from that producer. No formal archive
is migrated or rewritten; all-source historical OOS remains outside this closure.
