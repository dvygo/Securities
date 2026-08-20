"""counterToken: a per-venue positional counter in a reserved numeric block.

scriptToken carries each source's own instrument id, which is only unique within
that source -- Databento's instrument_id is unique within a dataset, so on
2026-08-12 the raw ids collided 932 times between XCME and XNAS because EQUS ids
start at 1 and run straight into GLBX's low ids. Anything keying on a token
without an exchange column needs something collision-free, and the pg
symbol-master table the plugin pushes to keys on exactly (token, trade_date).

So every venue is numbered into its own reserved block:

    counterToken = base * BLOCK + n,  n counting from 1 inside the block

This is arithmetic, not string concatenation. An earlier version glued the base
digit onto the counter, which made the token's width follow the counter's --
"1"+"35000" and "1"+"1" gave 135000 and 11, the same nominal base at wildly
different magnitudes, with no correct text ordering. Fixed-size blocks give every
token the same width and sort correctly as text or number.

Each venue owns TWO consecutive blocks and spills into the second when the first
fills, doubling the per-venue ceiling to 200M rows. Filling both raises rather
than wrapping: a wrapped counter would collide inside the venue's own trade_date,
which is the one failure this exists to prevent.

The numbering is positional and therefore per-day -- a contract gets a different
counterToken tomorrow if the universe shifts. That is intended, since the pg
primary key includes trade_date, but it means nothing may join on counterToken
across dates.

int32 budget: the highest allocated base is 12, whose block tops out at
1,300,000,000 -- 61% of int32's 2,147,483,647. Bases up to 20 stay inside it
(2,100,000,000), so there is room for four more venues before a 32-bit consumer
would overflow. Allocate beyond base 20 only after widening those consumers.
"""

BLOCK = 100_000_000

# venue -> the two blocks it owns. Keys match the per-venue output file's MIC
# prefix, since the counter has to be unique within a file, and one file is what
# the plugin step pushes.
#
#   XCBO   1,2    100000001 ..  300000000
#   XCME   3,4    300000001 ..  500000000
#   XNAS   5,6    500000001 ..  700000000
#   XNSE   7,8    700000001 ..  900000000
#   XBOM   9,10   900000001 .. 1100000000
#   XIMC  11,12  1100000001 .. 1300000000
#
# XNSE/XBOM are MIC bundles rather than single feeds -- XNSE merges NSE cash,
# F&O and currency; XBOM merges BSE cash and F&O -- so the block covers the whole
# merged file, which is the thing that has to be internally unique.
BASE = {
    "XCBO": (1, 2),
    "XCME": (3, 4),
    "XNAS": (5, 6),
    "XNSE": (7, 8),
    "XBOM": (9, 10),
    "XIMC": (11, 12),
}


def bases_for(venue: str):
    """Blocks owned by a venue, or None if it is not numbered."""
    return BASE.get((venue or "").upper())


def assign(bases, n: int) -> str:
    """counterToken for the n-th row (1-based) of a venue owning `bases` blocks."""
    block_index, offset = divmod(n - 1, BLOCK)
    if block_index >= len(bases):
        raise ValueError(
            f"counterToken blocks exhausted: row {n:,} needs block {block_index + 1} "
            f"but only {len(bases)} are allocated ({bases}). Widen counter_token.BASE "
            f"for this venue -- do not wrap, the tokens would collide."
        )
    return str(bases[block_index] * BLOCK + offset + 1)
