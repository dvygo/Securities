"""enable1/enable2: downstream gating flags, written as 0 by this pipeline.

The normalizer sets both to 0 on every row and never reads either. It has no
opinion about whether a contract is enabled, because it does not know what it
would be enabled for -- a downstream stage owns that, and further stages read
the result.

Filled at row-mapping time rather than in parquet_export, deliberately. That
module writes every column as a string and fills an absent key with "" on
purpose: "inferring types here would mean this module holding an opinion about
what a blank cell means". A default value is exactly that kind of opinion, so it
belongs with the code that knows what the column means.
"""
from typing import Any, Dict

from .. import paths

# What every enable column starts as. A string because the normalized parquet is
# all-string by design; ClickHouse types these Int64 on the way in.
DEFAULT = "0"


def fill(row: Dict[str, Any]) -> Dict[str, Any]:
    """Set every enable column the caller has not already set. Returns `row`."""
    for column in paths.ENABLE_COLUMNS:
        row.setdefault(column, DEFAULT)
    return row
