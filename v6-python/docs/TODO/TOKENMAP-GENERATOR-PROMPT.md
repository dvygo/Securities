Write a beginning-of-day job that converts a normalized contract-master parquet
into the binary token map a C++ market-data lane loads at startup.

## What the artifact is

`tokenmap.<VENUE>.bin` — a sorted `instrument_id -> counterTokenV2` lookup
table. The lane memory-maps it and probes it once per market-data record on a
pinned hot-path thread, so the format is fixed and the consumer casts it
directly. Do not invent a format; match the spec below byte for byte.

One map per venue. `instrument_id` is assigned PER DATASET by the vendor, so an
XNAS map loaded by an XCME lane would resolve every id to a real-looking token
belonging to a different contract — which is why the venue is stamped in the
header and the loader refuses a mismatch.

## Input

`<VENUE>-DATABENTO-normalized.parquet` — 92 columns, all stored as STRING.
You need exactly three:

| Column | Meaning |
| --- | --- |
| `def_raw_instrument_id` | the venue's own instrument id |
| `scriptToken` | the vendor-assigned id |
| `counterTokenV2` | the token to emit downstream |

Venues seen so far: XCME (~1,048,598 rows), XNAS (13,201), XCBO/OPRA
(2,002,550).

## Choosing the join column — this has a wrong answer that looks fine

Resolve by PRECEDENCE, never by "whichever has more values":

    def_raw_instrument_id   preferred, always, whenever it is populated
    scriptToken             fallback ONLY when def_raw is unusable

XCME and XCBO have the two columns byte-identical, so the rule is a no-op there.
**XNAS's master carries `def_raw_instrument_id = "0"` on all 13,201 rows**, with
only `scriptToken` populated — and `scriptToken` there (17218, 17220, …) lands
inside the live id range 1..24,146, so it is the id the feed actually sends.
That venue is the entire reason a fallback exists.

Do not pick the column with more distinct values. That is not evidence of being
the RIGHT column: a master where `def_raw` is correct but sparser would silently
flip to `scriptToken` and resolve every instrument to the wrong token — and the
file would still load and checksum cleanly.

If `def_raw_instrument_id` is PARTIALLY populated (neither empty nor whole),
FAIL rather than building from it. A half-filled map loads fine and leaves most
of the venue unresolvable at run time, which is a production-only failure.

Print which column was chosen and why. Accept an explicit override flag.

## Binary format

64-byte header, little-endian throughout, then the entry array.

| Offset | Size | Field |
| --- | --- | --- |
| 0  | 8 | magic, ASCII `MDFVTOK1` |
| 8  | 4 | uint32 format version = `1` |
| 12 | 4 | uint32 entry count |
| 16 | 8 | venue, ASCII, NUL-padded (`XCME`) |
| 24 | 8 | uint64 source row count (rows read from the parquet) |
| 32 | 8 | uint64 generated-at, unix nanoseconds |
| 40 | 4 | uint32 minimum id |
| 44 | 4 | uint32 maximum id |
| 48 | 8 | uint64 FNV-1a of the entry array |
| 56 | 8 | reserved, zero |
| 64 | count*8 | entries |

Each entry is 8 bytes: `uint32 instrument_id`, then `int32 token`.

Entries MUST be sorted strictly ascending by id. Not merely sorted — strictly:
a duplicate id makes the lookup order-dependent, and it means the master has two
rows claiming the same instrument, which is an upstream data problem, not
something to paper over by picking one. Fail on a duplicate.

## Rules a row must satisfy, or be skipped and counted

- id parses as uint32 and is **NOT ZERO**. Zero is the empty-slot marker in the
  consumer's open-addressed table, so a zero id would be indistinguishable from
  an empty slot. This is not optional.
- token parses and fits in **int32**, and is positive. Refuse rather than
  truncate if that ever stops being true.

Report skipped counts by reason. A silent skip shrinks coverage without saying
so.

## Checksum — get this exactly right

FNV-1a, 64-bit, over the entry array only (everything from offset 64 onward),
never the header.

    offset basis = 14695981039346656037     (0xcbf29ce484222325)
    prime        = 1099511628211            (0x100000001b3)

    h = offset_basis
    for each byte b:  h = (h XOR b) * prime, mod 2^64

**A digit was once dropped from that offset basis in a previous
implementation.** It is deterministic, so nothing ever failed and every
round-trip passed — the file was simply, self-consistently, not FNV-1a. It was
only caught by decoding the output with an INDEPENDENT implementation. Do the
same: verify with a decoder that does not share code with your writer.

## What the consumer validates at load

Your output must survive all of these, so test against them:

- magic is `MDFVTOK1`
- format version is exactly 1
- the venue in the header matches the lane's venue
- `file_size == 64 + count * 8`
- FNV-1a over the body matches the header value
- no entry has id 0
- entries are strictly ascending
- header min/max id equal the first and last entries

## Output

Write to a temp path and rename into place. An interrupted or failed run must
never leave a truncated file where the map belongs — the loader would reject it
on checksum, but only after an operator thought the job had succeeded.

Exit non-zero on any failure. Downstream must not start on a stale map.

## Verification you can regression-test against

Known-good outputs from the existing implementation, for masters dated
2026-08-26 (XNAS, XCBO) and 2026-08-27 (XCME):

| Venue | Key column | Entries | id range | FNV-1a |
| --- | --- | --- | --- | --- |
| XCME | def_raw_instrument_id | 1,048,598 | 2 .. 131,411,193 | `0x497527a8287fef1e` |
| XNAS | scriptToken (fallback) | 13,201 | 1 .. 24,124 | `0x26a9ce06fad03faa` |
| XCBO | def_raw_instrument_id | 2,002,550 | 1 .. 1,610,663,533 | `0x12dafb1a3ac9333a` |

If your generator reproduces those three checksums from the same masters, it is
byte-compatible.

## Reference implementation

If you have the market-data repo, read these rather than working from this
document alone:

- `tools/gen-tokenmap/main.go` — the existing generator, 227 lines, one
  dependency (`github.com/parquet-go/parquet-go v0.25.1`)
- `cmd/cpp-vendor/include/mdfv/tokenmap.hpp` — the consumer's lookup and the
  format constants
- `cmd/cpp-vendor/src/tokenmap.cpp` — the load-time validation above
- `cmd/cpp-vendor/tests/test_tokenmap.cpp` — 11 cases; the clearest statement of
  what a valid file is

The consumer is the contract. The writer is only correct if the loader accepts
it.

## Scope note

Build the generator only. Do not modify the C++ lane — it already loads this
format, and integration is being handled separately.
