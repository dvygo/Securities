#!/usr/bin/env python3
"""Run v4 symbology pipeline (US Databento Live + India Fyers HTTP).

US (Databento Live):
1. ``XCME-DATABENTO.py`` → ``YYYYMMDD/raw/XCME-DATABENTO.csv``
2. ``XCBO-DATABENTO.py`` → ``YYYYMMDD/raw/XCBO-DATABENTO.csv``
3. ``XNAS-DATABENTO.py`` → ``YYYYMMDD/raw/XNAS-DATABENTO.csv``

India (Fyers public sym_details):
4. ``XNSE/XNFO/XNCD/XBSE/XBFO/XMCX-FYERS.py`` → ``YYYYMMDD/raw/*.csv``

Post-process:
5. ``strip.py``       → ``YYYYMMDD/normalized/XCBO-DATABENTO.csv``
6. ``normalizer.py``  → enriches all ``YYYYMMDD/normalized/*.csv``
7. ``basket_refresh.py`` → ``constituents/contracts/YYYYMMDD/*.csv``
8. ``postgres-database-push.py`` → optional (``--postgres-push``)

  python runner.py
  python runner.py --postgres-push
  python runner.py --only fyers
  python runner.py --exclude databento
  python runner.py --exclude fyers normalize baskets
  python runner.py --only xnfo xbse normalize
  python runner.py --only baskets --date-dir 20260529
  python runner.py --dry-run

Extra CLI args (after ``--``) are passed only to Databento Live scripts (xcme/xcbo/xnas).

Ctrl+C stops the current step and exits (terminates child process if needed).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

_V4_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _V4_DIR.parent
_HELPERS_DIR = _V4_DIR / "helpers"
if str(_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPERS_DIR))

from symbology_paths import XCBO_CSV, XNSE_CSV, day_dir, repo_root

_DATABENTO_LIVE = frozenset({"xcme", "xcbo", "xnas"})
_FYERS_KEYS = frozenset({"xnse", "xnfo", "xncd", "xbse", "xbfo", "xmcx"})
# Steps that only make sense when their download group ran (or inputs already exist).
_EXCLUDE_IMPLICIT: dict[str, frozenset[str]] = {
    "databento": frozenset({"strip"}),
    "fyers": frozenset(),
}
# Skip a downstream step when required inputs are absent (e.g. prior run / partial day).
_STEP_PREREQ_FILES: dict[str, tuple[str, ...]] = {
    "strip": ("raw", XCBO_CSV),
    "baskets": ("normalized", XNSE_CSV),
}

if sys.platform == "win32":
    _CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
else:
    _CREATE_NEW_PROCESS_GROUP = 0

_BASE_STEPS: tuple[tuple[str, Path, bool], ...] = (
    ("xcme", _V4_DIR / "XCME-DATABENTO.py", True),
    ("xcbo", _V4_DIR / "XCBO-DATABENTO.py", True),
    ("xnas", _V4_DIR / "XNAS-DATABENTO.py", True),
    ("xnse", _V4_DIR / "XNSE-FYERS.py", False),
    ("xnfo", _V4_DIR / "XNFO-FYERS.py", False),
    ("xncd", _V4_DIR / "XNCD-FYERS.py", False),
    ("xbse", _V4_DIR / "XBSE-FYERS.py", False),
    ("xbfo", _V4_DIR / "XBFO-FYERS.py", False),
    ("xmcx", _V4_DIR / "XMCX-FYERS.py", False),
    ("strip", _HELPERS_DIR / "strip.py", False),
    ("normalize", _HELPERS_DIR / "normalizer.py", False),
    ("baskets", _HELPERS_DIR / "basket_refresh.py", False),
)

_POSTGRES_STEP: tuple[str, Path, bool] = (
    "postgres",
    _HELPERS_DIR / "postgres-database-push.py",
    False,
)

_child_proc: subprocess.Popen[bytes] | None = None


def _python_exe() -> Path:
    venv = _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return venv
    venv_unix = _REPO_ROOT / ".venv" / "bin" / "python"
    if venv_unix.is_file():
        return venv_unix
    return Path(sys.executable)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    prev = env.get("PYTHONPATH", "")
    extra_paths = [str(_V4_DIR.resolve()), str(_HELPERS_DIR.resolve())]
    extra = os.pathsep.join(extra_paths)
    env["PYTHONPATH"] = extra if not prev else f"{extra}{os.pathsep}{prev}"
    return env


def _terminate_child(*, force_after_sec: float = 6.0) -> None:
    global _child_proc
    proc = _child_proc
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError, AttributeError):
            pass
    else:
        try:
            proc.send_signal(signal.SIGTERM)
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=force_after_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _run_script(
    py: Path,
    script: Path,
    *,
    dry_run: bool,
    extra_argv: list[str],
) -> int:
    global _child_proc
    cmd = [str(py), str(script), *extra_argv]
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    if dry_run:
        return 0

    popen_kw: dict = {
        "cwd": str(_V4_DIR),
        "env": _env(),
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = _CREATE_NEW_PROCESS_GROUP

    _child_proc = subprocess.Popen(cmd, **popen_kw)
    try:
        return int(_child_proc.wait())
    except KeyboardInterrupt:
        print("\nrunner: interrupted, stopping current step...", file=sys.stderr, flush=True)
        _terminate_child()
        raise
    finally:
        _child_proc = None


def _expand_only(only: list[str] | None) -> set[str] | None:
    if not only:
        return None
    only_set = set(only)
    if "fyers" in only_set:
        only_set.discard("fyers")
        only_set.update(_FYERS_KEYS)
    return only_set


def _day_path(date_dir: str) -> Path:
    return day_dir(as_of=datetime.strptime(date_dir, "%Y%m%d").date(), root=repo_root())


def _prereq_missing(name: str, day: Path) -> str | None:
    spec = _STEP_PREREQ_FILES.get(name)
    if not spec:
        return None
    subdir, fname = spec[0], spec[1]
    path = day / subdir / fname
    if path.is_file():
        return None
    return path.name


def _apply_exclude(
    steps: list[tuple[str, Path, bool]],
    exclude: list[str],
) -> list[tuple[str, Path, bool]]:
    if not exclude:
        return steps
    skip: set[str] = set()
    for group in exclude:
        if group == "databento":
            skip.update(_DATABENTO_LIVE)
        elif group == "fyers":
            skip.update(_FYERS_KEYS)
        skip.update(_EXCLUDE_IMPLICIT.get(group, frozenset()))
    return [s for s in steps if s[0] not in skip]


def _build_steps(
    *,
    postgres_push: bool,
    only: list[str] | None,
    exclude: list[str],
) -> tuple[tuple[str, Path, bool], ...]:
    steps: list[tuple[str, Path, bool]] = list(_BASE_STEPS)
    if postgres_push or (only and "postgres" in only):
        steps.append(_POSTGRES_STEP)
    only_set = _expand_only(only)
    if only_set is not None:
        steps = [s for s in steps if s[0] in only_set]
    steps = _apply_exclude(steps, exclude)
    return tuple(steps)


def main(argv: list[str] | None = None) -> int:
    base_names = {name for name, _, _ in _BASE_STEPS}
    all_names = base_names | {"postgres", "fyers"}
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        nargs="+",
        choices=sorted(all_names),
        metavar="STEP",
        help=f"subset: {', '.join(sorted(all_names))}",
    )
    p.add_argument(
        "--exclude",
        nargs="+",
        choices=("databento", "fyers"),
        metavar="GROUP",
        default=[],
        help="skip groups: databento (xcme/xcbo/xnas/strip), fyers (six X*-FYERS downloads)",
    )
    p.add_argument(
        "--postgres-push",
        action="store_true",
        help="after normalize, load CSVs to Postgres primary (schema YYYYMMDD)",
    )
    p.add_argument(
        "--date-dir",
        default="",
        metavar="YYYYMMDD",
        help="passed to Fyers download / normalizer / postgres (default: today)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands only",
    )
    p.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="extra args for Databento Live only (prefix with -- e.g. -- --seconds 30)",
    )
    args = p.parse_args(argv)

    extra = list(args.extra)
    if extra and extra[0] == "--":
        extra = extra[1:]

    date_dir = (args.date_dir or "").strip() or date.today().strftime("%Y%m%d")
    selected = _build_steps(
        postgres_push=args.postgres_push,
        only=args.only,
        exclude=args.exclude,
    )
    if not selected:
        print("error: no steps selected (--only / --exclude)", file=sys.stderr)
        return 2

    py = _python_exe()
    if not args.dry_run:
        print(f"python: {py}", flush=True)
        print(f"cwd:    {_V4_DIR}", flush=True)
        needs_date = (
            args.postgres_push
            or (args.only and "postgres" in args.only)
            or (args.only and ("baskets" in args.only or "normalize" in args.only))
            or any(s[0] in _FYERS_KEYS for s in selected)
            or any(s[0] in ("normalize", "postgres", "baskets") for s in selected)
        )
        if needs_date:
            print(f"date-dir: {date_dir}", flush=True)
        if args.exclude:
            print(f"exclude: {', '.join(args.exclude)}", flush=True)

    day_path = _day_path(date_dir)
    rc = 0
    try:
        for name, script, forward_live in selected:
            if not script.is_file():
                print(f"error: missing {script}", file=sys.stderr)
                return 1
            miss = _prereq_missing(name, day_path)
            if miss:
                print(f"skip: {name} (missing {miss})", flush=True)
                continue
            argv_extra: list[str] = []
            if forward_live and name in _DATABENTO_LIVE:
                argv_extra = extra
            elif name in _FYERS_KEYS:
                argv_extra = ["--date-dir", date_dir]
                if args.dry_run:
                    argv_extra.append("--dry-run")
            elif name in ("strip", "normalize", "postgres", "baskets"):
                argv_extra = ["--date-dir", date_dir]
                if args.dry_run:
                    argv_extra.append("--dry-run")
            code = _run_script(py, script, dry_run=args.dry_run, extra_argv=argv_extra)
            if code != 0:
                print(f"error: {script.name} exited {code}", file=sys.stderr)
                rc = code
                break
    except KeyboardInterrupt:
        return 130

    if rc == 0 and not args.dry_run:
        print("\nrunner: all steps finished OK", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
