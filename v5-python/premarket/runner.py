"""Pipeline runner: Step class, --only expansion, sequential execution."""
import re
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class Step:
    """A pipeline step with a name and execution function."""
    name: str
    run: Callable[["Opts"], None]


@dataclass
class Opts:
    """Options passed to step runners."""
    as_of: str  # YYYYMMDD
    date_dir: str  # YYYYMMDD
    dry_run: bool = False
    input_path: Optional[str] = None
    database_url: Optional[str] = None
    basket: Optional[str] = None
    include_csv_header: bool = False
    all_symbols: bool = False
    symbols_file: Optional[str] = None
    stype_in: Optional[str] = None
    live_start: Optional[str] = None
    hist_range: Optional[str] = None  # raw 16-digit YYYYMMDDYYYYMMDD, unparsed


_RANGE_RE = re.compile(r"^(\d{8})(\d{8})$")


def parse_hist_range(range_str: str) -> tuple[str, str]:
    """Parse 16-digit YYYYMMDDYYYYMMDD into (from, to) YYYYMMDD strings."""
    m = _RANGE_RE.match(range_str)
    if not m:
        raise ValueError(f"--range must be 16 digits YYYYMMDDYYYYMMDD, got: {range_str}")
    return m.group(1), m.group(2)


def expand_only(only: List[str], all_steps: List[Step]) -> List[Step]:
    """
    Expand --only aliases and return filtered steps.
    Aliases (from Go's runner.go):
      - fyers, databento, nse: shorthand for multiple steps
      - live, hist: mode filters
      - all: all steps
      - normalize: all normalize steps
    """
    if not only or "all" in only:
        return all_steps

    # Step name -> canonical name mapping
    aliases = {
        "fyers": ["normalize-fyers"],
        "databento": ["normalize-databento"],
        "nse": ["normalize-nse"],
        "india": ["download-india"],
        "xcme": ["download-xcme"],
        "xcbo": ["download-xcbo"],
        "xnas": ["download-xnas"],
        "live": ["download-india-live", "download-xcme-live", "download-xcbo-live", "download-xnas-live"],
        "hist": ["download-india-hist", "download-xcme-hist", "download-xcbo-hist", "download-xnas-hist"],
        "normalize": [
            "normalize-fyers",
            "normalize-nse",
            "normalize-databento",
            "strip",
            "baskets",
        ],
    }

    expanded = set()
    for item in only:
        if item in aliases:
            expanded.update(aliases[item])
        else:
            expanded.add(item)

    # Filter steps to only those in expanded
    filtered = [s for s in all_steps if s.name in expanded]

    # Preserve order from all_steps
    return [s for s in all_steps if s in filtered]


def run(steps: List[Step], opts: Opts) -> int:
    """
    Execute steps sequentially.
    Returns 0 on success, 1 on first error.
    """
    for step in steps:
        print(f">>> {step.name}", file=sys.stderr)
        sys.stderr.flush()
        try:
            step.run(opts)
        except Exception as e:
            print(f"error: {step.name}: {e}", file=sys.stderr)
            return 1
    return 0


def build_normalizer_steps(only: List[str], postgres_push: bool = False) -> List[Step]:
    """
    Build the normalizer pipeline steps.
    Returns Step list filtered by --only.
    """
    from . import postgres_export, baskets, export
    from .normalize import fields, databento_norm, nse_norm

    all_steps = [
        Step("normalize-fyers", fields.run),
        Step("normalize-nse", nse_norm.run),
        Step("normalize-databento", databento_norm.run),
        Step("strip", fields.run_strip),  # placeholder, may be separate
        Step("baskets", baskets.run),
        Step("csv-export", export.run),
    ]

    if postgres_push:
        all_steps.append(Step("postgres", postgres_export.run))

    return expand_only(only, all_steps)


def build_download_steps(
    venue: str,  # "india", "xcme", "xcbo", "xnas"
    mode: Optional[str] = None,  # "live" or "hist" or None for both
) -> List[Step]:
    """Build download steps for a given venue."""
    from .sources import fyers_src, databento_src

    if venue == "india":
        return [
            Step("download-india", lambda opts: fyers_src.download(opts)),
        ]
    elif venue == "xcme":
        steps = []
        if not mode or mode == "hist":
            steps.append(Step("download-xcme-hist", lambda opts: databento_src.download(opts, "xcme", "hist")))
        if not mode or mode == "live":
            steps.append(Step("download-xcme-live", lambda opts: databento_src.download(opts, "xcme", "live")))
        return steps
    elif venue == "xcbo":
        steps = []
        if not mode or mode == "hist":
            steps.append(Step("download-xcbo-hist", lambda opts: databento_src.download(opts, "xcbo", "hist")))
        if not mode or mode == "live":
            steps.append(Step("download-xcbo-live", lambda opts: databento_src.download(opts, "xcbo", "live")))
        return steps
    elif venue == "xnas":
        steps = []
        if not mode or mode == "hist":
            steps.append(Step("download-xnas-hist", lambda opts: databento_src.download(opts, "xnas", "hist")))
        if not mode or mode == "live":
            steps.append(Step("download-xnas-live", lambda opts: databento_src.download(opts, "xnas", "live")))
        return steps
    else:
        raise ValueError(f"Unknown venue: {venue}")
