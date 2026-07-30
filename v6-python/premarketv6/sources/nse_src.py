"""NSE exchange file reader (NEW FILE FORMAT)."""
import csv
from pathlib import Path
from typing import Dict, List, Optional

from .. import paths


# NSE "NEW FILE FORMAT" header constants
NSE_HEADERS = {
    "FinInstrmId": "FinInstrmId",
    "TckrSymb": "TckrSymb",
    "XpryDt": "XpryDt",
    "StrkPric": "StrkPric",
    "OptnTp": "OptnTp",
    "ISIN": "ISIN",
    "TickSz": "TickSz",
    "LotSz": "LotSz",
    "UndlyingSym": "UndlyingSym",
    "UndlyingId": "UndlyingId",
}


def read_csv(path: Path) -> List[Dict[str, str]]:
    """
    Read NSE "NEW FILE FORMAT" CSV.
    Returns list of row dicts keyed by header name.
    Raises error on empty file.
    """
    if not path.exists():
        raise FileNotFoundError(f"NSE CSV not found: {path}")

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"Empty NSE CSV file: {path}")

        rows = list(reader)
        if not rows:
            raise ValueError(f"No data rows in NSE CSV: {path}")

        return rows


def read_all_segments(date_dir: str) -> Dict[str, List[Dict[str, str]]]:
    """Read all NSE segments for a given day."""
    nse_dir = paths.nse_exchange_raw_dir(date_dir)
    result = {}

    for segment_name, filename in paths.NSE_SEGMENTS.items():
        segment_path = nse_dir / filename
        try:
            result[segment_name] = read_csv(segment_path)
        except FileNotFoundError:
            result[segment_name] = []
        except ValueError as e:
            print(f"Warning: {e}")
            result[segment_name] = []

    return result
