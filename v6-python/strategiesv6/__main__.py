"""CLI entry point: python -m strategiesv6 --strategy=str04

Dispatches to strategiesv6/<NAME>/loader.py:main() for the named strategy, so
each strategy's beginning-of-day loader shares one invocation convention
instead of growing its own argparse boilerplate.

Arguments this parser does not recognise are forwarded to the loader rather
than rejected, so a strategy that needs its own flags can define them without
every other strategy having to know. A loader opts in by taking an argv
parameter; the ones that take none keep working untouched.
"""
import argparse
import importlib
import inspect
import sys


def main() -> int:
    # add_help=False, and --strategy not required, so that -h can be routed:
    # "--strategy=str06 --help" has to reach STR06's parser, while a bare
    # "--help" still describes this dispatcher. argparse's built-in help would
    # intercept the first case and print the wrong usage.
    parser = argparse.ArgumentParser(
        prog="strategiesv6",
        description="Run a strategy's beginning-of-day loader",
        add_help=False,
    )
    parser.add_argument(
        "--strategy",
        help="Strategy folder name, e.g. str04 (case-insensitive)",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="want_help")
    args, extra = parser.parse_known_args()

    if not args.strategy:
        parser.print_help()
        # Asking for help is a request that was served; naming no strategy is not.
        return 0 if args.want_help else parser.error("the following arguments are required: --strategy")

    if args.want_help:
        extra.append("--help")

    name = args.strategy.strip().upper()
    try:
        mod = importlib.import_module(f"strategiesv6.{name}.loader")
    except ModuleNotFoundError as e:
        raise SystemExit(f"Unknown strategy {args.strategy!r}: no strategiesv6/{name}/loader.py ({e})")

    if inspect.signature(mod.main).parameters:
        return mod.main(extra) or 0
    if extra:
        # Silently dropping them would make a typo'd flag look like it worked.
        raise SystemExit(f"{name} takes no options of its own; unrecognised: {' '.join(extra)}")
    return mod.main() or 0


if __name__ == "__main__":
    sys.exit(main())
