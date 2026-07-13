"""CLI entry point: python -m strategies --strategy=str04

Dispatches to strategies/<NAME>/loader.py:main() for the named strategy, so
each strategy's beginning-of-day loader shares one invocation convention
instead of growing its own argparse boilerplate.
"""
import argparse
import importlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="strategies",
        description="Run a strategy's beginning-of-day loader",
    )
    parser.add_argument(
        "--strategy",
        required=True,
        help="Strategy folder name, e.g. str04 (case-insensitive)",
    )
    args = parser.parse_args()

    name = args.strategy.strip().upper()
    try:
        mod = importlib.import_module(f"strategies.{name}.loader")
    except ModuleNotFoundError as e:
        raise SystemExit(f"Unknown strategy {args.strategy!r}: no strategies/{name}/loader.py ({e})")

    return mod.main() or 0


if __name__ == "__main__":
    sys.exit(main())
