"""Command-line interface with subcommands."""
import argparse
import sys
from datetime import datetime

from . import runlog, runner
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
        "--basket",
        help="Specific basket to refresh",
    )
    normalize_parser.add_argument(
        "--csv",
        dest="csv_export_dir",
        help="Export aggregated CSVs to directory",
    )

    return parser


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
            return run_download(args.command, args)
        elif args.command == "normalize":
            return run_normalize(args)
        else:
            parser.print_help()
            return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
