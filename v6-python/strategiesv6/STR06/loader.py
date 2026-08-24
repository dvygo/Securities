"""STR06: submit a Databento batch job for GLBX.MDP3 ALL_SYMBOLS trades.

    python -m strategiesv6 --strategy=str06 [--start ...] [--end ...] [--submit]

Trades for every instrument CME lists is a large single request -- 4.67M
records / 0.22 GB for the 2026-08-21 session, and proportionally more for a
range. The batch API is the supported route for it: the request is queued
server-side, Databento assembles the files, and they are downloaded once ready.
Nothing is streamed or held in memory here.

Every submission is recorded in manifest.json next to this file: the request as
sent, the acknowledgement verbatim, and the UTC time it was submitted. That file
is how a job is found again later -- `--download` with no argument resolves the
most recent entry -- so the job id never has to be kept anywhere else.

Because a batch job is billable and cannot be un-submitted, this previews by
default and only submits when told to. A bare run prints the record count,
billable size and cost for the exact request --submit would send, then exits.

Uses premarketv6.config's XCME key (DATABENTO_KEY_XCME env var, or conf/keys.ini
key_XCME, selected via DATABENTO_ENV) -- GLBX.MDP3 is what that key is
provisioned for.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import databento as db

from premarketv6 import config as premarket_config

DATASET = "GLBX.MDP3"
SCHEMA = "trades"
VENUE_KEY = "XCME"

# ALL_SYMBOLS bypasses symbology entirely, so stype_in only labels the request.
# raw_symbol is what premarketv6's own ALL_SYMBOLS path sends for GLBX; parent
# would return identical records but mislabel what the files contain.
SYMBOLS = "ALL_SYMBOLS"
STYPE_IN = "raw_symbol"

ENCODING = "dbn"

# zstd, so files arrive as *.dbn.zst. Uncompressed is tempting for a single
# session -- 2026-08-21 was 397.7 MB -- but the free rolling year is ~68.8 GB
# billable, which lands near 120 GB of plain .dbn across ~250 daily files. At
# that size the transfer, not the decode, is the cost. --compression none asks
# for literal .dbn instead; the download filter accepts either suffix.
COMPRESSION = "zstd"

# One file per session, so a multi-day request arrives as separate .dbn files and
# a failed download resumes at a day boundary instead of restarting a single
# enormous file.
SPLIT_DURATION = "day"

# Data files only. A finished job also carries metadata.json, condition.json and
# symbology.json (confirmed on GLBX-20260822-PARKQBWBCR); downloading the job as
# a whole pulls the lot as one zip, so files are fetched individually by name.
DATA_SUFFIXES = (".dbn", ".dbn.zst")

STRATEGY_DIR = Path(__file__).resolve().parent
DATA_DIR = STRATEGY_DIR / "data"
MANIFEST_PATH = STRATEGY_DIR / "manifest.json"

UTC = dt.timezone.utc


# ------------------------------------------------------------
# MANIFEST
# ------------------------------------------------------------
def load_manifest() -> Dict[str, Any]:
    """Read manifest.json, or an empty manifest if there is none yet."""
    if not MANIFEST_PATH.exists():
        return {"jobs": []}
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(
            f"{MANIFEST_PATH} is unreadable ({e}). Refusing to continue rather than "
            f"overwrite it -- it is the only record of what was submitted."
        )
    data.setdefault("jobs", [])
    return data


def save_manifest(manifest: Dict[str, Any]) -> None:
    """Write manifest.json atomically.

    Staged then renamed: this file is the only local record that a billable job
    was submitted, and a crash mid-write would otherwise lose every prior entry
    along with the one being added.
    """
    temp_path = MANIFEST_PATH.with_name(f"{MANIFEST_PATH.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
        f.write("\n")
    temp_path.replace(MANIFEST_PATH)


def record_submission(request: Dict[str, Any], ack: Dict[str, Any]) -> None:
    """Append one submission to the manifest.

    The acknowledgement is stored verbatim rather than picked apart into chosen
    fields: whatever Databento returns today is what a later lookup gets, and a
    field added or renamed server-side does not silently vanish from the record.
    """
    manifest = load_manifest()
    manifest["jobs"].append({
        "job_id": ack.get("id", ""),
        "requested_at": dt.datetime.now(UTC).isoformat(),
        "request": request,
        "ack": ack,
        "downloads": [],
    })
    save_manifest(manifest)


def find_entry(manifest: Dict[str, Any], job_id: Optional[str]) -> Dict[str, Any]:
    """The manifest entry for a job id, or the most recent submission."""
    jobs = manifest.get("jobs", [])
    if not jobs:
        raise SystemExit(
            f"No submissions recorded in {MANIFEST_PATH}. Submit one first with --submit."
        )
    if job_id is None:
        return jobs[-1]
    for entry in jobs:
        if entry.get("job_id") == job_id:
            return entry
    raise SystemExit(f"Job {job_id} is not in {MANIFEST_PATH}")


# ------------------------------------------------------------
# RANGE
# ------------------------------------------------------------
def latest_complete_session(client: db.Historical) -> dt.date:
    """Last session date the dataset has complete data for.

    get_dataset_range()["end"] is an EXCLUSIVE bound carrying a mid-session
    timestamp (e.g. 2026-08-21T05:20Z), so its own date is a partial session.
    Step back a day to land on one that is actually finished -- the same
    correction premarketv6's definition download makes, for the same reason.
    """
    available_end = client.metadata.get_dataset_range(dataset=DATASET)["end"]
    return dt.datetime.strptime(available_end[:10], "%Y-%m-%d").date() - dt.timedelta(days=1)


def resolve_range(client: db.Historical, start: Optional[str], end: Optional[str]) -> tuple:
    """(start, end) as ISO dates. Defaults to one lookback day.

    `end` is exclusive, matching the API, so the default single session is
    start=D, end=D+1. One day rather than a window is deliberate: at GLBX
    ALL_SYMBOLS trades volume an accidental week is a very expensive typo.
    """
    if start:
        start_date = dt.datetime.strptime(start, "%Y-%m-%d").date()
    else:
        start_date = latest_complete_session(client)

    end_date = dt.datetime.strptime(end, "%Y-%m-%d").date() if end else start_date + dt.timedelta(days=1)

    if end_date <= start_date:
        raise SystemExit(
            f"--end {end_date.isoformat()} must be after --start {start_date.isoformat()} "
            f"(end is exclusive, so a single session is start=D end=D+1)"
        )
    return start_date.isoformat(), end_date.isoformat()


# ------------------------------------------------------------
# JOB
# ------------------------------------------------------------
def build_request(start: str, end: str, compression: str) -> Dict[str, Any]:
    """The exact submit_job arguments, also stored in the manifest."""
    return dict(
        dataset=DATASET,
        symbols=SYMBOLS,
        schema=SCHEMA,
        stype_in=STYPE_IN,
        start=start,
        end=end,
        encoding=ENCODING,
        compression=compression,
        split_duration=SPLIT_DURATION,
        delivery="download",
    )


def preview(client: db.Historical, request: Dict[str, Any]) -> None:
    """Print what the job would cost, from the request that would be submitted."""
    query = {
        k: request[k] for k in ("dataset", "symbols", "schema", "stype_in", "start", "end")
    }
    print(f"  records       : {client.metadata.get_record_count(**query):,}")
    print(f"  billable size : {client.metadata.get_billable_size(**query) / 1e9:.2f} GB")
    print(f"  cost          : ${client.metadata.get_cost(**query):,.2f}")


def print_jobs(client: db.Historical) -> None:
    """Show every recorded submission, with its state refreshed from the API.

    Reads the manifest rather than batch.list_jobs() so a job stays listed after
    Databento expires it -- the record of what was asked for outlives the data.
    """
    manifest = load_manifest()
    jobs = manifest.get("jobs", [])
    if not jobs:
        print(f"  no submissions recorded in {MANIFEST_PATH}")
        return
    for entry in jobs:
        job_id = entry.get("job_id", "?")
        try:
            state = client.batch.get_job_details(job_id).get("state", "?")
        except Exception as e:
            # An expired or purged job 404s; that is information, not a failure.
            state = f"unavailable ({str(e)[:40]})"
        request = entry.get("request", {})
        downloaded = len(entry.get("downloads", []))
        print(
            f"  {job_id}  {state:<12} {request.get('start', '?')}..{request.get('end', '?')} "
            f"submitted {entry.get('requested_at', '?')[:19]} "
            f"({downloaded} file(s) downloaded)"
        )


def data_filenames(client: db.Historical, job_id: str) -> List[str]:
    """The job's .dbn files, excluding the metadata/condition/symbology sidecars."""
    files = client.batch.list_files(job_id)
    return sorted(
        str(f["filename"]) for f in files
        if str(f.get("filename", "")).endswith(DATA_SUFFIXES)
    )


def download_job(client: db.Historical, job_id: str, out_dir: Path) -> List[Path]:
    """Download only the job's data files, one call per file.

    batch.download() with no filename fetches the whole job as a single zip and
    extracts everything in it, sidecars included. Naming each file is the only
    way to take the .dbn files alone.
    """
    filenames = data_filenames(client, job_id)
    if not filenames:
        raise SystemExit(
            f"No {' or '.join(DATA_SUFFIXES)} files in job {job_id}. It may still be "
            f"processing -- check with --list."
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    # The SDK writes to {output_dir}/{job_id}/{filename}; knowing that is what
    # lets an interrupted pull resume. A year of GLBX trades is ~250 files, so
    # restarting from zero after a dropped connection is not an option.
    job_dir = out_dir / job_id
    sizes = {str(f["filename"]): int(f.get("size") or 0) for f in client.batch.list_files(job_id)}

    pending = []
    for name in filenames:
        local = job_dir / name
        expected = sizes.get(name, 0)
        # Size must match, not merely exist: a file cut short by a killed download
        # would otherwise be treated as complete and silently leave a hole.
        if local.exists() and expected and local.stat().st_size == expected:
            continue
        pending.append(name)

    done_already = len(filenames) - len(pending)
    if done_already:
        print(f"  {len(filenames)} data file(s), {done_already} already complete", flush=True)
    else:
        print(f"  {len(filenames)} data file(s)", flush=True)

    written: List[Path] = []
    for i, name in enumerate(pending, 1):
        paths = client.batch.download(
            job_id=job_id, output_dir=out_dir, filename_to_download=name
        )
        for path in paths:
            mb = path.stat().st_size / 1e6 if path.exists() else 0
            print(f"    [{i}/{len(pending)}] {path.name}  {mb:,.1f} MB", flush=True)
        written.extend(paths)
    return written


def record_downloads(job_id: str, paths: List[Path]) -> None:
    """Note on the manifest entry what was pulled, and when."""
    manifest = load_manifest()
    for entry in manifest.get("jobs", []):
        if entry.get("job_id") != job_id:
            continue
        entry.setdefault("downloads", []).append({
            "downloaded_at": dt.datetime.now(UTC).isoformat(),
            "files": [str(p) for p in paths],
        })
        save_manifest(manifest)
        return


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strategiesv6 --strategy=str06",
        description=f"Submit a Databento batch job: {DATASET} {SYMBOLS} {SCHEMA}",
    )
    parser.add_argument("--start", help="Session date YYYY-MM-DD (default: latest complete session)")
    parser.add_argument("--end", help="Exclusive end date YYYY-MM-DD (default: start + 1 day)")
    parser.add_argument(
        "--compression",
        choices=("none", "zstd"),
        default=COMPRESSION,
        help=f"Delivered file compression (default: {COMPRESSION}; "
             f"none gives .dbn, zstd gives .dbn.zst)",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit. Without this the job is only priced, not sent.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Show recorded submissions with their current state, and exit",
    )
    parser.add_argument(
        "--download", nargs="?", const="", metavar="JOB_ID",
        help="Download a job's .dbn files (default: the most recent submission)",
    )
    parser.add_argument(
        "--output-dir", default=str(DATA_DIR),
        help=f"Download destination (default: {DATA_DIR})",
    )
    args = parser.parse_args(argv)

    cfg = premarket_config.load_databento()
    api_key = cfg.keys.get(VENUE_KEY, "")
    if not api_key:
        raise SystemExit(
            f"Missing Databento key: set DATABENTO_KEY_{VENUE_KEY} env var, or "
            f"key_{VENUE_KEY} in conf/keys.ini."
        )
    client = db.Historical(api_key)

    if args.list:
        print_jobs(client)
        return 0

    if args.download is not None:
        entry = find_entry(load_manifest(), args.download or None)
        job_id = entry["job_id"]
        print(f"STR06: downloading {job_id} -> {args.output_dir}", flush=True)
        written = download_job(client, job_id, Path(args.output_dir))
        record_downloads(job_id, written)
        print(f"DONE: {len(written)} file(s)")
        return 0

    start, end = resolve_range(client, args.start, args.end)
    request = build_request(start, end, args.compression)
    print(f"STR06: {DATASET} {SYMBOLS} {SCHEMA}, {start}..{end} (end exclusive)", flush=True)
    preview(client, request)

    if not args.submit:
        print("\n  Preview only -- nothing submitted. Re-run with --submit to send this job.")
        return 0

    ack = client.batch.submit_job(**request)
    record_submission(request, ack)
    job_id = ack.get("id", "?")
    print(f"\n  submitted : {job_id}  state={ack.get('state', '?')}")
    print(f"  recorded  : {MANIFEST_PATH}")
    print("  poll      : python -m strategiesv6 --strategy=str06 --list")
    print("  fetch     : python -m strategiesv6 --strategy=str06 --download")
    return 0


if __name__ == "__main__":
    sys.exit(main())
