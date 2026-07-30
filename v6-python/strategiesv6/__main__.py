"""CLI entry point: python -m strategiesv6 --strategy=str04

Dispatches to strategiesv6/<NAME>/loader.py:main() for the named strategy, so
each strategy's beginning-of-day loader shares one invocation convention
instead of growing its own argparse boilerplate.
"""
import argparse
import importlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="strategiesv6",
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
        mod = importlib.import_module(f"strategiesv6.{name}.loader")
    except ModuleNotFoundError as e:
        raise SystemExit(f"Unknown strategy {args.strategy!r}: no strategiesv6/{name}/loader.py ({e})")

    return mod.main() or 0


if __name__ == "__main__":
    sys.exit(main())
