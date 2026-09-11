"""NSE's own contract masters: confirm the broker's daily drop is complete.

This is the sibling of `fyers-india`, and deliberately not a download. Nothing
here touches the network -- there is no NSE source URL to fetch from. The broker
drops the exchange's files into the XNSE venue directory each day, and
normalize/nse_contract.py builds XNSE-NSE.parquet from them:

    data/YYYYMMDD/XNSE/NEW FILE FORMAT/
        NSE_CM_security.csv    cash market      -> equities, ETFs, debt, G-secs
        NSE_FO_contract.csv    futures/options  -> FUTIDX FUTSTK OPTIDX OPTSTK
        NSE_CD_contract.csv    currency derivs  -> FUTCUR OPTCUR FUTIRC FUTIRT

The other files that arrive in that folder (contract.txt, security.txt, the two
spdcontract files, fo_participant.txt) are ignored on purpose; see
nse_contract.py for why.

normalize-nse-contract already tolerates a missing drop -- it prints a line and
moves on, because a normalize run covers many venues and one absent venue must
not sink the rest. That is the right behaviour there and the wrong one for an
operator asking "did today's NSE files land?". This step answers that question
directly and fails the run when the answer is no.
"""
from .. import runner
from ..normalize import nse_contract

# Order is the order they are reported in, and matches nse_contract.py.
REQUIRED = (nse_contract.CM_FILE, nse_contract.FO_FILE, nse_contract.CD_FILE)


def run(opts: runner.Opts) -> None:
    """Report each required file's row count; raise if any is missing or empty."""
    directory = nse_contract.drop_dir(opts.date_dir)
    print(f"  drop: {directory}")

    if not directory.is_dir():
        raise FileNotFoundError(
            f"no NSE contract drop for {opts.date_dir} -- expected {directory}")

    problems = []
    for name in REQUIRED:
        path = directory / name
        if not path.exists():
            print(f"  {name:<22} MISSING")
            problems.append(f"{name} missing")
            continue
        # Counted through nse_contract's own reader so this agrees with what
        # normalize-nse-contract will actually ingest, header handling included.
        rows = sum(1 for _ in nse_contract._read(path))
        if rows == 0:
            print(f"  {name:<22} {rows:>9,} rows  EMPTY")
            problems.append(f"{name} has no rows")
        else:
            print(f"  {name:<22} {rows:>9,} rows  OK")

    if problems:
        raise RuntimeError(
            f"NSE contract drop incomplete ({'; '.join(problems)})")
    print("  drop complete")
