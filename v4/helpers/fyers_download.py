#!/usr/bin/env python3
"""Download Fyers public symbol-master CSVs into ``YYYYMMDD/raw/{MIC}-FYERS.csv``.

Source files have no header row (21 columns as of 2026; legacy 17-column files padded on read).

  python fyers_download.py --segment xnfo
  python fyers_download.py --segment xnse --date-dir 20260529
  python fyers_download.py --all
  python fyers_download.py --segment xbse --input path/to/NSE_FO.csv

Uses ``[fyers]`` in ``../secrets/secrets.ini`` (base URL, user-agent, timeout, retries).
"""

from __future__ import annotations

import argparse
import configparser
import csv
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from symbology_paths import (
    FYERS_BASE_URL,
    FYERS_RAW_COLUMNS,
    FYERS_SEGMENTS,
    config_ini,
    fyers_segment,
    raw_csv,
    repo_root,
)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _load_fyers_config() -> dict[str, str]:
    defaults = {
        "base_url": FYERS_BASE_URL,
        "user_agent": _DEFAULT_UA,
        "timeout_sec": "120",
        "retries": "3",
        "retry_delay_sec": "2",
    }
    ini = config_ini()
    if not ini.is_file():
        return defaults
    cp = configparser.ConfigParser()
    cp.read(ini, encoding="utf-8")
    if not cp.has_section("fyers"):
        return defaults
    sec = cp["fyers"]
    for k in defaults:
        if sec.get(k, fallback="").strip():
            defaults[k] = sec.get(k, fallback="").strip()
    return defaults


def _source_url(seg_key: str, cfg: dict[str, str]) -> str:
    seg = fyers_segment(seg_key)
    base = (cfg.get("base_url") or FYERS_BASE_URL).rstrip("/")
    return f"{base}/{seg.source_file}"


def _fetch_bytes(url: str, cfg: dict[str, str]) -> bytes:
    timeout = float(cfg.get("timeout_sec") or "120")
    retries = int(cfg.get("retries") or "3")
    delay = float(cfg.get("retry_delay_sec") or "2")
    ua = cfg.get("user_agent") or _DEFAULT_UA
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt < retries:
                print(
                    f"retry {attempt}/{retries} for {url}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
    raise RuntimeError(f"download failed for {url}: {last_err}") from last_err


def _parse_headerless_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    ncols = len(FYERS_RAW_COLUMNS)
    legacy = ncols - 4
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        row = next(csv.reader([s]))
        if len(row) == legacy:
            row = [*row, "", "", "", ""]
        elif len(row) != ncols:
            raise ValueError(
                f"expected {legacy} or {ncols} fields, got {len(row)}: {row[:5]!r}...",
            )
        rows.append(row)
    return rows


def _write_headered_csv(path: Path, rows: list[list[str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=str(path.parent), prefix=f".{path.stem}_")
    try:
        with open(fd, "w", newline="", encoding="utf-8-sig") as fout:
            w = csv.writer(fout)
            w.writerow(FYERS_RAW_COLUMNS)
            w.writerows(rows)
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(rows)


def download_segment(
    seg_key: str,
    *,
    as_of: date | None = None,
    date_dir: str = "",
    input_path: Path | None = None,
    dry_run: bool = False,
) -> Path:
    seg = fyers_segment(seg_key)
    cfg = _load_fyers_config()
    if date_dir.strip():
        day = date_dir.strip()
        try:
            as_of = datetime.strptime(day, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"--date-dir must be YYYYMMDD, got {day!r}") from exc
    out = raw_csv(seg.output_csv, as_of=as_of, root=repo_root())

    if dry_run:
        if input_path is not None:
            print(f"dry-run: {seg.key} <- {input_path} -> {out}", file=sys.stderr)
        else:
            print(f"dry-run: {seg.key} <- {_source_url(seg.key, cfg)} -> {out}", file=sys.stderr)
        return out

    if input_path is not None:
        src = input_path if input_path.is_absolute() else (repo_root() / input_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(src)
        data = src.read_bytes()
    else:
        url = _source_url(seg.key, cfg)
        print(f"download: {url}", file=sys.stderr, flush=True)
        data = _fetch_bytes(url, cfg)

    rows = _parse_headerless_csv(data)
    n = _write_headered_csv(out, rows)
    print(f"wrote {n} rows -> {out}", file=sys.stderr, flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    keys = [s.key for s in FYERS_SEGMENTS]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--segment", choices=keys, help="Fyers segment to download")
    p.add_argument("--all", action="store_true", help="download all six segments")
    p.add_argument("--date-dir", default="", metavar="YYYYMMDD", help="output day folder")
    p.add_argument(
        "--input",
        type=Path,
        default=None,
        help="use local headerless CSV instead of HTTP (manual fallback)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.all:
        selected = keys
    elif args.segment:
        selected = [args.segment]
    else:
        p.error("specify --segment or --all")

    for key in selected:
        try:
            download_segment(
                key,
                date_dir=args.date_dir,
                input_path=args.input,
                dry_run=args.dry_run,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: {key}: {exc}", file=sys.stderr)
            return 1
    return 0


def main_segment(seg_key: str, argv: list[str] | None = None) -> int:
    extra = list(argv or [])
    if "--segment" not in extra:
        extra = ["--segment", seg_key, *extra]
    return main(extra)


if __name__ == "__main__":
    raise SystemExit(main())
