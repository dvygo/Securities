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
    basket: Optional[str] = None
    include_csv_header: bool = False
    all_symbols: bool = False
    symbols_file: Optional[str] = None
    stype_in: Optional[str] = None
    live_start: Optional[str] = None
    hist_range: Optional[str] = None  # raw 16-digit YYYYMMDDYYYYMMDD, unparsed
    # --venue: restrict the per-venue steps to these MICs (uppercased). Empty
    # means every venue config.ini enables. Narrows a run; it cannot widen one,
    # so a venue with enabled = 0 stays off even when named here.
    venues: tuple = ()


def venue_selected(opts: "Opts", mic: str) -> bool:
    """Whether `mic` is in this run's --venue selection. True when none was given."""
    return not opts.venues or mic.upper() in opts.venues


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
            "normalize-nse-contract",
            "normalize-databento",
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
    if not steps:
        print("error: --only matched no steps", file=sys.stderr)
        return 1
    for step in steps:
        print(f">>> {step.name}", file=sys.stderr)
        sys.stderr.flush()
        try:
            step.run(opts)
        except Exception as e:
            print(f"error: {step.name}: {e}", file=sys.stderr)
            return 1
    return 0


def build_normalizer_steps(
    only: List[str],
    contracts_push_only: bool = False,
    plugin: bool = False,
    csv_only: bool = False,
) -> List[Step]:
    """
    Build the normalizer pipeline steps.
    Returns Step list filtered by --only.

    contracts_push_only adds the ClickHouse contracts push to an otherwise normal
    run -- the CSVs are still normalized and written first. It does not narrow the
    pipeline to the writers.

    csv_only is its opposite and wins over it: it drops every step that writes to
    a database, so a run can produce the CSVs for inspection without touching
    ClickHouse or Postgres. That is a veto rather than a preference, and it also
    beats naming a db step in --only, because the failure it prevents (an unwanted
    write to a live table) cannot be undone by re-running.
    """
    from . import clickhouse_export, config, baskets, export

    # [baskets].enabled, and only there -- there is no flag for it. Whether a
    # deployment builds baskets is a property of the deployment, not of whoever
    # is typing the command, so a scheduled run cannot change it by forgetting
    # to repeat an argument.
    baskets_enabled = config.load_baskets().enabled
    from .normalize import fields, databento_norm, nse_norm, nse_contract
    from .plugin import build as plugin_build, postgres as plugin_postgres

    all_steps = [
        Step("normalize-fyers", fields.run),
        Step("normalize-nse", nse_norm.run),
        Step("normalize-nse-contract", nse_contract.run),
        Step("normalize-databento", databento_norm.run),
    ]
    # Dropped outright rather than left in to run and write nothing, so a run
    # with baskets off is visible as a shorter step list.
    if baskets_enabled:
        all_steps.append(Step("baskets", baskets.run))
    all_steps.append(Step("csv-export", export.run))

    if plugin:
        all_steps.append(Step("plugin", plugin_build.run))
        # Building plugin CSVs otherwise pushes them too -- --csv-only is the
        # opt-out.
        if not csv_only:
            all_steps.append(Step("postgres-plugin", plugin_postgres.run))

    # The contracts push is ClickHouse now (see clickhouse_export). postgres-plugin
    # above is unaffected and still writes to Postgres.
    if contracts_push_only and not csv_only:
        all_steps.append(Step("clickhouse", clickhouse_export.run))

    steps = expand_only(only, all_steps)

    if csv_only:
        # Say what was suppressed rather than silently returning fewer steps: a
        # user who typed "--only postgres --csv-only" needs to know the push did
        # not merely succeed quietly.
        asked = {s for s in only if s in ("clickhouse", "postgres-plugin")}
        if asked:
            print(f"--csv-only: skipping {', '.join(sorted(asked))}", file=sys.stderr)

    # Same courtesy as --csv-only above: "--only baskets" against a config that
    # disables them is an empty run, and the reason is in a file rather than on
    # the command line, so say which.
    if not baskets_enabled and "baskets" in only:
        print("[baskets] enabled = 0 in config.ini: skipping baskets", file=sys.stderr)

    return steps


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
