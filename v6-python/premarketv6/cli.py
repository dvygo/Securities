"""Command-line interface with subcommands."""
import argparse
import sys
from datetime import datetime

from . import config, paths, runlog, runner
from .sources import databento_src


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="premarketv6",
        description="Pre-market symbology/instrument data pipeline",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Common args for download commands
    def add_download_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--date-dir",
            default=datetime.now().strftime("%Y%m%d"),
            help="Date directory (YYYYMMDD, default: today)",
        )
        subparser.add_argument(
            "--dry-run",
            action="store_true",
            help="Don't write files, just simulate",
        )
        subparser.add_argument(
            "--hist",
            action="store_true",
            help="Download historical symbology data",
        )
        subparser.add_argument(
            "--live",
            action="store_true",
            help="Download live streaming data",
        )
        subparser.add_argument(
            "--range",
            dest="hist_range",
            help="Hist date range, 16 digits YYYYMMDDYYYYMMDD (from,to). Ignored with --live.",
        )
        subparser.add_argument(
            "--only",
            action="append",
            default=[],
            help="Only run specific steps (can be repeated)",
        )
        subparser.add_argument(
            "--input",
            dest="input_path",
            help="Input file path (for local testing)",
        )
        subparser.add_argument(
            "--include-csv-header",
            action="store_true",
            help="Include CSV header in output",
        )

    # India (Fyers) subcommand
    india_parser = subparsers.add_parser("india", help="Download Fyers Indian exchange data")
    add_download_args(india_parser)

    # Databento venue subcommands. The venue set, and whether --all-symbols is
    # on by default, both come from config.ini's [EXCHANGE:<CODE>] sections --
    # see premarketv6.config.load_exchanges. Nothing about a venue is hardcoded
    # here any more, so adding one is a config edit.
    for venue, exchange_cfg in sorted(databento_src.VENUE_CONFIGS.items()):
        venue_parser = subparsers.add_parser(
            venue, help=f"Download {exchange_cfg.venue_name} Databento data")
        add_download_args(venue_parser)
        venue_parser.add_argument(
            "--all-symbols",
            action=argparse.BooleanOptionalAction,
            default=exchange_cfg.all_symbols_default,
            help=(f"Subscribe to ALL_SYMBOLS "
                  f"(default: {str(exchange_cfg.all_symbols_default).lower()}; "
                  f"--no-all-symbols uses the basket CSV)"),
        )
        venue_parser.add_argument(
            "--symbols-file",
            help="Path to symbols file",
        )
        venue_parser.add_argument(
            "--dates",
            help="Comma-separated YYYYMMDD list to backfill, e.g. "
                 "--dates=20260827,20260825,20260101. One batch job per date, all "
                 "submitted before any is waited on. Each date lands in its own "
                 "venue directory. Mutually exclusive with --today/--date-dir.",
        )
        venue_parser.add_argument(
            "--today",
            action="store_true",
            help="Today's date. The default already, so this only states it "
                 "explicitly -- useful next to --dates in a script.",
        )

    # Normalize subcommand
    normalize_parser = subparsers.add_parser("normalize", help="Normalize downloaded data")
    normalize_parser.add_argument(
        "--date-dir",
        default=datetime.now().strftime("%Y%m%d"),
        help="Date directory (YYYYMMDD, default: today)",
    )
    normalize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write files, just simulate",
    )
    normalize_parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only run specific normalize steps (normalize-fyers, normalize-databento, normalize-nse, baskets, csv-export, plugin, postgres-plugin, clickhouse)",
    )
    normalize_parser.add_argument(
        "--clickhouse-push-only",
        dest="contracts_push_only",
        action="store_true",
        help="Push contracts/baskets to ClickHouse after normalization (files are still built first)",
    )
    normalize_parser.add_argument(
        "--plugin",
        action="store_true",
        help="Build plugin-format Parquet (legacy pg symbol-master schema) in data/YYYYMMDD/v6/plugin/",
    )
    normalize_parser.add_argument(
        "--csv-only",
        action="store_true",
        help="Write files only, never touch a database. Overrides --clickhouse-push-only "
             "and suppresses the clickhouse/postgres-plugin steps even if named in --only.",
    )
    normalize_parser.add_argument(
        "--venue",
        action="append",
        default=[],
        metavar="MIC",
        help="Restrict the per-venue steps (normalize-databento, normalize-fyers, "
             "plugin) to this MIC; repeatable. Baskets and csv-export aggregate "
             "across venues and are not narrowed. Narrows only -- a venue with "
             "enabled = 0 stays off even when named.",
    )
    normalize_parser.add_argument(
        "--basket",
        help="Specific basket to refresh",
    )
    normalize_parser.add_argument(
        "--csv",
        dest="csv_export_dir",
        help="Export aggregated CSVs to directory",
    )

    # check-tokens subcommand
    check_parser = subparsers.add_parser(
        "check-tokens",
        help="Validate counterTokenV2 against the normalized parquet and manifests already written",
    )
    check_parser.add_argument(
        "--dates",
        default=datetime.now().strftime("%Y%m%d"),
        help="Comma-separated YYYYMMDD to validate (default: today). Consecutive "
             "pairs are also checked against each other, which is where the "
             "carry-forward contract actually lives -- pass the whole week.",
    )
    check_parser.add_argument(
        "--venue",
        action="append",
        default=[],
        metavar="MIC",
        help="Restrict to this MIC; repeatable. Every venue with a normalized "
             "file is checked when omitted.",
    )

    # check-lineage subcommand
    lineage_parser = subparsers.add_parser(
        "check-lineage",
        help="Trace each stage's output back to the stage before it: raw DBN -> "
             "normalized -> plugin",
    )
    lineage_parser.add_argument(
        "--dates",
        default=datetime.now().strftime("%Y%m%d"),
        help="Comma-separated YYYYMMDD to trace (default: today). Each day is "
             "independent -- unlike check-tokens, nothing here spans two dates.",
    )
    lineage_parser.add_argument(
        "--venue",
        action="append",
        default=[],
        metavar="MIC",
        help="Restrict to this MIC; repeatable.",
    )

    # migrate-manifests subcommand
    migrate_parser = subparsers.add_parser(
        "migrate-manifests",
        help="Convert v3 venue manifests (allocation inline in the JSON) to the "
             "v4 header + Parquet allocation table",
    )
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be converted without writing anything.",
    )

    return parser


def run_backfill(venue: str, args: argparse.Namespace, dates: tuple) -> int:
    """--dates: one definition batch job per date, all submitted before any is awaited."""
    import databento as db
    from .sources import databento_src

    cleanup, log_path = runlog.setup(f"premarketv6-{venue}", dates[0])
    try:
        print(f"Log: {log_path}", file=sys.stderr)
        venue_cfg = databento_src.VENUE_CONFIGS[venue]
        if not venue_cfg.enabled:
            raise SystemExit(
                f"{venue} ({venue_cfg.venue_name}) is disabled: set enabled = 1 in "
                f"config.ini [EXCHANGE:{venue_cfg.venue_name}] to download it")

        # --dates is the ALL_SYMBOLS definition path and nothing else. The basket
        # route resolves symbols per day against a different API and has no batch
        # job to track, so accepting --dates there would silently mean something
        # entirely different.
        if not getattr(args, "all_symbols", venue_cfg.all_symbols_default):
            raise SystemExit("--dates requires --all-symbols (it drives the definition batch path)")

        print(f"Backfilling {venue_cfg.venue_name} for {len(dates)} date(s): {', '.join(dates)}")
        if args.dry_run:
            for d in dates:
                print(f"DRY RUN: would submit a {venue_cfg.dataset} definition job for {d} "
                      f"-> {paths.manual_venue_dir(d, venue_cfg.venue_name)}")
            return 0

        cfg = config.load_databento()
        api_key = cfg.keys.get(venue_cfg.venue_name, "")
        if not api_key:
            raise SystemExit(
                f"No Databento API key for {venue} ({venue_cfg.venue_name}); "
                f"set key_{venue_cfg.venue_name} in conf/keys.ini")

        client = db.Historical(key=api_key)
        written = databento_src.download_definitions_for_dates(
            client, venue_cfg, "raw_symbol", dates)
        total = sum(written.values())
        print(f"Backfill complete: {len(written)} date(s), {total:,} byte(s)")
        return 0
    finally:
        cleanup()


def run_download(venue: str, args: argparse.Namespace) -> int:
    """Run download command for a venue."""
    cleanup, log_path = runlog.setup(f"premarketv6-{venue}", args.date_dir)
    try:
        print(f"Log: {log_path}", file=sys.stderr)

        opts = runner.Opts(
            as_of=args.date_dir,
            date_dir=args.date_dir,
            dry_run=args.dry_run,
            input_path=getattr(args, "input_path", None),
            include_csv_header=getattr(args, "include_csv_header", False),
            all_symbols=getattr(args, "all_symbols", False),
            symbols_file=getattr(args, "symbols_file", None),
            stype_in=getattr(args, "stype_in", None),
            live_start=getattr(args, "live_start", None),
            hist_range=getattr(args, "hist_range", None),
        )

        # Determine mode from flags; default to hist if neither specified
        hist_flag = getattr(args, "hist", False)
        live_flag = getattr(args, "live", False)
        if hist_flag and live_flag:
            mode = None  # Both modes
        elif live_flag:
            mode = "live"
        else:
            mode = "hist"  # Default to hist

        steps = runner.build_download_steps(venue, mode=mode)
        only = getattr(args, "only", []) or []
        if only:
            steps = runner.expand_only(only, steps)

        return runner.run(steps, opts)
    finally:
        cleanup()


def run_normalize(args: argparse.Namespace) -> int:
    """Run normalize command."""
    cleanup, log_path = runlog.setup("normalizer", args.date_dir)
    try:
        print(f"Log: {log_path}", file=sys.stderr)

        opts = runner.Opts(
            as_of=args.date_dir,
            date_dir=args.date_dir,
            dry_run=args.dry_run,
            basket=getattr(args, "basket", None),
            venues=_venue_selection(getattr(args, "venue", []) or []),
        )

        only = getattr(args, "only", []) or []
        # Asking for the clickhouse/plugin step by name is itself the request to
        # run it; --clickhouse-push-only/--plugin only matter for a full run with
        # no --only filter. postgres-plugin has no flag of its own -- it rides
        # along with --plugin (or is targeted directly via --only).
        contracts_push_only = getattr(args, "contracts_push_only", False) or "clickhouse" in only
        plugin = args.plugin or "plugin" in only or "postgres-plugin" in only

        steps = runner.build_normalizer_steps(
            only,
            contracts_push_only=contracts_push_only,
            plugin=plugin,
            csv_only=getattr(args, "csv_only", False),
        )

        return runner.run(steps, opts)
    finally:
        cleanup()


def _date_list(raw: str) -> tuple:
    """Parse --dates into a tuple of YYYYMMDD, newest first, rejecting junk.

    Newest first because a backfill is usually wanted most-recent-first, and
    because the definition_ready_ratio check in _prepare_batch_window compares
    against the prior session -- hitting the newest date first surfaces an
    unpublished today before the older jobs are submitted.
    """
    seen, out = set(), []
    for part in str(raw).split(","):
        stamp = part.strip()
        if not stamp:
            continue
        try:
            datetime.strptime(stamp, "%Y%m%d")
        except ValueError:
            raise SystemExit(
                f"--dates: {stamp!r} is not a YYYYMMDD date. "
                f"Expected e.g. --dates=20260827,20260825"
            )
        if stamp not in seen:
            seen.add(stamp)
            out.append(stamp)
    if not out:
        raise SystemExit("--dates was given but contained no dates")
    return tuple(sorted(out, reverse=True))


def _venue_selection(raw: list) -> tuple:
    """Normalise --venue values to a tuple of MICs, rejecting unknown ones.

    A typo has to fail loudly. Passed through silently, "--venue XNSA" would
    match no venue, every per-venue step would find nothing to do, and the run
    would report success having written nothing -- indistinguishable from a day
    where the data genuinely had not arrived.
    """
    if not raw:
        return ()
    known = {code.upper() for code in config.load_exchanges()}
    picked, unknown = [], []
    for value in raw:
        for part in str(value).split(","):
            mic = part.strip().upper()
            if not mic:
                continue
            (picked if mic in known else unknown).append(mic)
    if unknown:
        raise SystemExit(
            f"Unknown venue(s): {', '.join(sorted(set(unknown)))}. "
            f"Venues come from config.ini [EXCHANGE:<CODE>] sections; "
            f"configured: {', '.join(sorted(known)) or '(none)'}"
        )
    return tuple(dict.fromkeys(picked))


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "india":
            return run_download("india", args)
        elif args.command in databento_src.VENUE_CONFIGS:
            raw_dates = getattr(args, "dates", None)
            if raw_dates and getattr(args, "today", False):
                raise SystemExit("--dates and --today are mutually exclusive")
            if raw_dates:
                return run_backfill(args.command, args, _date_list(raw_dates))
            return run_download(args.command, args)
        elif args.command == "normalize":
            return run_normalize(args)
        elif args.command == "check-tokens":
            from .qa import tokens
            return tokens.run(_date_list(args.dates), _venue_selection(args.venue))
        elif args.command == "check-lineage":
            from .qa import lineage
            return lineage.run(_date_list(args.dates), _venue_selection(args.venue))
        elif args.command == "migrate-manifests":
            from .normalize import migrate_manifest
            return migrate_manifest.run(dry_run=args.dry_run)
        else:
            parser.print_help()
            return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
