#!/usr/bin/env python3
"""Run v3 Databento Live symbology pipeline.

1. ``glbx_mdp3.py``   → ``YYYYMMDD/glbx_mdp3.csv``
2. ``opra_pillar.py`` → ``YYYYMMDD/opra_pillar_unstripped.csv``
3. ``equs_mini.py``   → ``YYYYMMDD/equs_mini.csv`` (dataset ``EQUS.MINI``)
4. ``strip.py``       → ``YYYYMMDD/opra_pillar.csv``
5. ``normalizer.py``  → enriches CSVs with normalized columns
6. ``postgres-database-push.py`` → optional (``--postgres-push``) loads into Postgres primary

  python runner.py
  python runner.py --postgres-push
  python runner.py --only equs
  python runner.py --only equs glbx
  python runner.py --only opra
  python runner.py --only normalize postgres --postgres-push
  python runner.py --dry-run

Uses repo ``.venv`` when present, else current ``python``.
Extra CLI args (after ``--``) are passed only to the three Live scripts, not strip / normalizer / postgres.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

_V3_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _V3_DIR.parent

_BASE_STEPS: tuple[tuple[str, str, bool], ...] = (
    ("glbx", "glbx_mdp3.py", True),
    ("opra", "opra_pillar.py", True),
    ("equs", "equs_mini.py", True),
    ("strip", "strip.py", False),
    ("normalize", "normalizer.py", False),
)

_POSTGRES_STEP: tuple[str, str, bool] = ("postgres", "postgres-database-push.py", False)


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
    extra = str(_V3_DIR.resolve())
    env["PYTHONPATH"] = extra if not prev else f"{extra}{os.pathsep}{prev}"
    return env


def _run_script(
    py: Path,
    script: Path,
    *,
    dry_run: bool,
    extra_argv: list[str],
) -> int:
    cmd = [str(py), str(script), *extra_argv]
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    if dry_run:
        return 0
    r = subprocess.run(
        cmd,
        cwd=str(_V3_DIR),
        env=_env(),
    )
    return int(r.returncode)


def _build_steps(
    *,
    postgres_push: bool,
    only: list[str] | None,
) -> tuple[tuple[str, str, bool], ...]:
    steps: list[tuple[str, str, bool]] = list(_BASE_STEPS)
    if postgres_push or (only and "postgres" in only):
        steps.append(_POSTGRES_STEP)
    if only:
        only_set = set(only)
        steps = [s for s in steps if s[0] in only_set]
    return tuple(steps)


def main(argv: list[str] | None = None) -> int:
    base_names = {name for name, _, _ in _BASE_STEPS}
    all_names = base_names | {"postgres"}
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        nargs="+",
        choices=sorted(all_names),
        metavar="STEP",
        help=f"subset: {', '.join(sorted(all_names))}",
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
        help="passed to normalizer/postgres steps (default: today)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands only",
    )
    p.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="extra args for Live scripts only (prefix with -- e.g. -- --seconds 30)",
    )
    args = p.parse_args(argv)

    extra = list(args.extra)
    if extra and extra[0] == "--":
        extra = extra[1:]

    date_dir = (args.date_dir or "").strip() or date.today().strftime("%Y%m%d")
    selected = _build_steps(postgres_push=args.postgres_push, only=args.only)

    py = _python_exe()
    if not args.dry_run:
        print(f"python: {py}", flush=True)
        print(f"cwd:    {_V3_DIR}", flush=True)
        if args.postgres_push or (args.only and "postgres" in args.only):
            print(f"date-dir: {date_dir}", flush=True)

    rc = 0
    for name, script_name, forward_live in selected:
        script = _V3_DIR / script_name
        if not script.is_file():
            print(f"error: missing {script}", file=sys.stderr)
            return 1
        argv_extra: list[str] = []
        if forward_live:
            argv_extra = extra
        elif name in ("normalize", "postgres"):
            argv_extra = ["--date-dir", date_dir]
            if args.dry_run:
                argv_extra.append("--dry-run")
        code = _run_script(py, script, dry_run=args.dry_run, extra_argv=argv_extra)
        if code != 0:
            print(f"error: {script_name} exited {code}", file=sys.stderr)
            rc = code
            break

    if rc == 0 and not args.dry_run:
        print("\nrunner: all steps finished OK", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
