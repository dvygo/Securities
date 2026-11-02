"""Broker symbology: derive the brokerScript1..4 columns.

brokerScript1 renders a contract in the broker's own symbology:

    equity          AAPL                       -> AAPL                (copy of script)
    OPRA option     META  260918C00705000      -> META/260918/705C
    GLBX option     E1BQ6 C6130                -> E1B/Q26/6130C
    GLBX future     ESZ6                       -> ES/Z26
    spread / combo  ESH7-ESZ7                  -> ESH7-ESZ7           (copy of script)
                    UD:1V: GN 2533155          -> UD:1V: GN 2533155   (copy of script)

Anything this module cannot decompose falls back to an exact copy of `script`,
so the column is never empty for a row that has a script.

brokerScript2/3/4 are reserved for other brokers and are always empty for now.
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# The four columns appended to paths.NORMALIZED_COLUMNS, in order.
BROKER_SCRIPT_COLUMNS = ["brokerScript1", "brokerScript2", "brokerScript3", "brokerScript4"]

# Only brokerScript1 is derived today; the rest stay blank until their broker
# formats are specified.
UNSPECIFIED_BROKER_SCRIPT_COLUMNS = BROKER_SCRIPT_COLUMNS[1:]

# Trailing CME month code + single-digit year on a GLBX base, e.g. "ESZ6" ->
# ("ES", "Z", "6") and "E1BQ6" -> ("E1B", "Q", "6"). The root is non-greedy so
# it backtracks until the last two characters are a valid month/year pair.
GLBX_BASE_REGEX = re.compile(r"^(?P<root>.+?)(?P<month>[FGHJKMNQUVXZ])(?P<year>\d)$")

# GLBX weekly-option suffix, e.g. "E1BQ6 C6130" -> ("C", "6130"). Mirrors
# databento_norm.GLBX_OPTION_REGEX; duplicated here so this module stays
# importable without a circular import back into the mapper.
GLBX_OPTION_REGEX = re.compile(r"\s+([CP])(\d+(?:\.\d+)?)\s*$")

# A GLBX base containing any of these is a spread or exchange-defined combo
# ("ESH7-ESZ7", "UD:1V: GN 2533155"), not a single contract. Without this guard
# "ESH7-ESZ7" would match GLBX_BASE_REGEX as root "ESH7-ES" + month Z + year 7
# and silently produce the nonsense "ESH7-ES/Z27".
GLBX_COMBO_MARKERS = ("-", ":", " ")


def format_strike(value: Any) -> str:
    """Render a strike with no trailing zeros: 705.0 -> "705", 302.5 -> "302.5"."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{f:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _year2_from_expiration_ns(expiration_ns: Any) -> Optional[str]:
    """Two-digit UTC year from a GLBX CONTRACT MONTH timestamp (epoch ns).

    GLBX symbols encode only a single-digit year ("ESZ6"), which is ambiguous
    across decades, so the caller resolves the decade once and passes the
    resolved contract month in (databento_norm.glbx_expiration_ns).

    Deliberately not the row's `expiration` column: that is the venue's last
    eligible trade time, which for a December contract can fall in January and
    would name the contract a year late. 0BZ0 expires 2031-01-01 and is 0B/Z30.
    """
    try:
        ns = int(float(expiration_ns))
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    return f"{datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).year % 100:02d}"


def from_equity(script: str) -> str:
    """Equities carry no contract terms; the broker symbol is the ticker itself."""
    return script or ""


def from_occ(script: str, parsed: Optional[Dict[str, Any]]) -> str:
    """OPRA/OCC option -> "<ROOT>/<YYMMDD>/<STRIKE><C|P>".

    `parsed` is databento_norm.parse_occ_symbol(script) output; its `expiration`
    is a YYYYMMDD string taken straight from the OCC symbol. That date is used
    rather than the row's `expiration` column, which has already been shifted to
    the session close in UTC and can therefore land on a different calendar day.
    """
    if not parsed:
        return script or ""
    root = (parsed.get("underlying") or "").upper()
    yyyymmdd = parsed.get("expiration") or ""
    option_type = parsed.get("option_type") or ""
    if not root or len(yyyymmdd) != 8 or option_type not in ("CALL", "PUT"):
        return script or ""
    cp = "C" if option_type == "CALL" else "P"
    return f"{root}/{yyyymmdd[2:]}/{format_strike(parsed.get('strike', 0))}{cp}"


def from_glbx(script: str, expiration_ns: Any) -> str:
    """GLBX future -> "<ROOT>/<MONTH><YY>"; GLBX option -> "<ROOT>/<MONTH><YY>/<STRIKE><C|P>".

    Falls back to an exact copy of `script` for spreads, exchange-defined
    combos, "parent" entries carrying no contract month, and any row whose
    expiration is missing (so the decade cannot be resolved).
    """
    s = (script or "").strip()
    if not s:
        return ""

    opt_match = GLBX_OPTION_REGEX.search(s)
    if opt_match:
        base = s[: opt_match.start()].strip()
        cp, strike = opt_match.group(1), opt_match.group(2)
    else:
        base, cp, strike = s, None, None

    if any(marker in base for marker in GLBX_COMBO_MARKERS):
        return script
    base_match = GLBX_BASE_REGEX.match(base)
    if not base_match:
        return script

    year2 = _year2_from_expiration_ns(expiration_ns)
    if year2 is None:
        return script

    contract = f"{base_match.group('root')}/{base_match.group('month')}{year2}"
    return f"{contract}/{format_strike(strike)}{cp}" if cp else contract


def fill_unspecified(result: Dict[str, Any]) -> Dict[str, Any]:
    """Set brokerScript2/3/4 to empty on a row being built."""
    for col in UNSPECIFIED_BROKER_SCRIPT_COLUMNS:
        result[col] = ""
    return result
