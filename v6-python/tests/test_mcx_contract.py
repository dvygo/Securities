"""MCX's own contract file (normalize/mcx_contract.py), which serves XIMC.

The rows here are built column by column from the layout worked out on the
2026-10-28 drop, so each test states the column it depends on. What a
regression would break silently: the Unix dates, the paise prices, the lot
column, naming an option by its OWN expiry month, the spread filter, and the
session close moving with US daylight saving.
"""
import datetime as dt
import zipfile

import pyarrow.parquet as pq
import pytest

from premarketv6 import paths, runner
from premarketv6.normalize import mcx_contract as mc

AS_OF = "20261028"
AS_OF_DAY = dt.date(2026, 10, 28)


def _eod(day: dt.date) -> int:
    """How the file stores an expiry: the day at 23:59:59 UTC."""
    return int(dt.datetime(day.year, day.month, day.day, 23, 59, 59,
                           tzinfo=dt.timezone.utc).timestamp())


def row(token="513461", symbol="GOLD", inst="OPTFUT", expiry=dt.date(2026, 10, 30),
        strike="16600000", option="CE", lot="1", tick="50", group="PRECIOUS_METAL",
        product="OFGOLD", desc="GOLD 995", file_day=dt.date(2026, 10, 15)):
    """One 118-column MCXScrips.bcp row with the columns the reader uses."""
    r = ["0"] * mc.WIDTH
    r[mc.COL_FILE_TS] = str(_eod(file_day) - 75_000)
    r[mc.COL_TOKEN] = token
    r[mc.COL_SYMBOL] = symbol
    r[mc.COL_EXPIRY] = str(_eod(expiry)) if expiry else "0"
    r[mc.COL_LOT] = lot
    r[mc.COL_TICK] = tick
    r[mc.COL_DESC] = desc
    r[mc.COL_GROUP] = group
    r[mc.COL_INST] = inst
    r[mc.COL_STRIKE] = strike if inst.startswith("OPT") else "0"
    r[mc.COL_OPTION] = option if inst.startswith("OPT") else "XX"
    r[mc.COL_PRODUCT_CODE] = product
    r[mc.COL_CURRENCY] = "INR"
    return r


def future(**kw):
    kw.setdefault("inst", "FUTCOM")
    kw.setdefault("product", "FUGOLD")
    kw.setdefault("tick", "100")
    return row(**kw)


def spot(**kw):
    kw.update(inst="COM", expiry=None, product=kw.get("symbol", "GOLD"))
    kw.setdefault("token", "114")
    return row(**kw)


def write_zip(tree, rows, name="ZippedDownload.zip", member=mc.MEMBER, as_of=AS_OF):
    """The BOD drop: one zip in data/YYYYMMDD/MCX/, with a file the reader ignores."""
    folder = tree / as_of / mc.DROP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, "\r\n".join(",".join(r) for r in rows) + "\r\n")
        archive.writestr("MCX_PART.bcp", "TESTCP1,A BROKER,A,2025-09-03 11:54:37,1\r\n")
    return path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def mcx_feed(tmp_path, monkeypatch):
    ini = tmp_path / "config.ini"
    ini.write_text("[EXCHANGE:XIMC]\nfeed = mcx\nenabled = 1\nvenue_id = 20\n")
    monkeypatch.setenv("PREMARKET_CONFIG", str(ini))


def opts(dry_run=False):
    return runner.Opts(as_of=AS_OF, date_dir=AS_OF, dry_run=dry_run)


# -- dates ---------------------------------------------------------------

def test_dates_are_unix_seconds_not_the_nse_epoch():
    """1793404799 is 2026-10-30 23:59:59 UTC. Read with NSE's 1980 offset it
    would be 2036 -- the file has no such offset."""
    assert mc._utc_date(1793404799) == dt.date(2026, 10, 30)


def test_expiry_lands_on_the_session_close_and_moves_with_us_daylight_saving():
    """30 Oct 2026: US still on daylight time, close 23:30 IST = 18:00Z.
    30 Dec 2026: US on standard time, close 23:55 IST = 18:25Z."""
    oct30 = mc.expiration_ns(_eod(dt.date(2026, 10, 30)), mc.METALS_ENERGY)
    dec30 = mc.expiration_ns(_eod(dt.date(2026, 12, 30)), mc.METALS_ENERGY)
    assert oct30 == int(dt.datetime(2026, 10, 30, 18, 0, tzinfo=dt.timezone.utc).timestamp()) * 10**9
    assert dec30 == int(dt.datetime(2026, 12, 30, 18, 25, tzinfo=dt.timezone.utc).timestamp()) * 10**9


def test_agri_closes_at_five_whatever_the_season():
    for day in (dt.date(2026, 10, 30), dt.date(2026, 12, 30)):
        got = mc.expiration_ns(_eod(day), mc.AGRI)
        assert got == int(dt.datetime(day.year, day.month, day.day, 11, 30,
                                      tzinfo=dt.timezone.utc).timestamp()) * 10**9


def test_a_group_outside_the_table_keeps_the_files_own_time():
    eod = _eod(dt.date(2026, 11, 5))
    assert mc.expiration_ns(eod, None) == eod * 10**9


def test_a_spot_row_has_no_expiry():
    assert mc.expiration_ns(0, mc.METALS_ENERGY) == 0


# -- sessions ------------------------------------------------------------

@pytest.mark.parametrize("profile,summer,winter", [
    (mc.METALS_ENERGY, "0330-1800", "0330-1825"),
    (mc.AGRI,          "0330-1130", "0330-1130"),
    (mc.INTL_AGRI,     "0330-1530", "0330-1600"),
])
def test_session_by_profile_and_season(profile, summer, winter):
    assert mc.session_utc(profile, dt.date(2026, 10, 28)) == summer
    assert mc.session_utc(profile, dt.date(2026, 11, 2)) == winter   # first Monday after the US change


def test_the_international_agri_symbols_override_their_group():
    """COTTON is AGRO_PRODUCTS in the file but trades the evening session."""
    assert mc.profile_for("AGRO_PRODUCTS", "COTTON") == mc.INTL_AGRI
    assert mc.profile_for("AGRO_PRODUCTS", "COTTONCNDY") == mc.AGRI
    assert mc.profile_for("OTHERS", "MENTHAOIL") == mc.INTL_AGRI
    assert mc.profile_for("PRECIOUS_METAL", "GOLD") == mc.METALS_ENERGY
    assert mc.profile_for("SOMETHING_NEW", "X") is None


def test_an_unknown_group_gets_a_blank_session():
    assert mc.session_utc(None, AS_OF_DAY) == ""


# -- names ---------------------------------------------------------------

def test_an_option_is_named_by_its_own_expiry_month():
    """Two GOLD option series on the same DEC2026 future, expiring 30 Oct and
    27 Nov. Column 104 says DEC2026 for both; their own months tell them apart."""
    oct_ = mc.script_for(row(expiry=dt.date(2026, 10, 30)))
    nov_ = mc.script_for(row(token="513496", expiry=dt.date(2026, 11, 27)))
    assert oct_ == "GOLD26OCT166000CE"
    assert nov_ == "GOLD26NOV166000CE"


def test_futures_and_spot_names():
    assert mc.script_for(future(expiry=dt.date(2027, 8, 5))) == "GOLD27AUGFUT"
    assert mc.script_for(spot()) == "GOLD"


def test_a_fractional_strike_keeps_its_decimals():
    assert mc.strike_text(159050) == "1590.5"
    assert mc.strike_text(16600000) == "166000"
    assert mc.strike_text(0) == ""


# -- prices and quantities -----------------------------------------------

def test_prices_stay_in_paise_and_the_lot_comes_from_column_20():
    out = mc.map_row(row(lot="1", tick="50", strike="16600000"), AS_OF_DAY)
    assert out["multiplier"] == 100
    assert out["strike"] == 16600000            # Rs 1,66,000
    assert out["tickSize"] == 50                # Rs 0.50
    assert out["lotSize"] == 1                  # 1 KGS
    assert out["optionType"] == "CALL"
    assert out["scriptInstrumentType2"] == "OPTION"
    assert out["scriptDetails"] == "GOLD 30 Oct 26 166000 CE"
    copper = mc.map_row(future(symbol="COPPER", lot="2500", tick="5",
                               group="NON_FERROUS_METAL", expiry=dt.date(2026, 11, 28)), AS_OF_DAY)
    assert copper["lotSize"] == 2500


def test_a_spot_row_maps_with_its_description():
    out = mc.map_row(spot(desc="GOLD 995"), AS_OF_DAY)
    assert out["scriptDetails"] == "GOLD 995"
    assert out["expiration"] == 0 and out["strike"] == 0
    assert out["scriptInstrumentType2"] == "COM"


# -- what is kept --------------------------------------------------------

def test_spreads_and_expired_contracts_are_dropped(tree):
    rows = [
        row(),                                                         # live option
        future(expiry=AS_OF_DAY, token="2"),                           # expires today: kept
        future(expiry=dt.date(2026, 10, 27), token="3"),               # expired yesterday
        future(symbol="GLDDECFE27", product="0", token="132243721",
               expiry=dt.date(2026, 12, 4)),                           # calendar spread
        spot(),
    ]
    zip_path = write_zip(tree, rows)
    kept, stats = mc.rows_for(AS_OF, zip_path)
    assert [k["scriptToken"] for k in kept] == ["513461", "2", "114"]
    assert stats["expired"] == 1 and stats["spreads"] == 1
    assert stats["file_date"] == dt.date(2026, 10, 15)


def test_a_row_of_the_wrong_width_is_counted_not_read(tree):
    zip_path = write_zip(tree, [row(), row()[:100]])
    kept, stats = mc.rows_for(AS_OF, zip_path)
    assert len(kept) == 1 and stats["malformed"] == 1


def test_an_unhandled_instrument_type_is_counted(tree):
    zip_path = write_zip(tree, [row(), future(inst="SPRCOM", token="9")])
    kept, stats = mc.rows_for(AS_OF, zip_path)
    assert len(kept) == 1 and stats["unknown_type"] == {"SPRCOM": 1}


def test_a_repeated_name_keeps_the_first(tree):
    zip_path = write_zip(tree, [row(token="1"), row(token="2")])
    kept, stats = mc.rows_for(AS_OF, zip_path)
    assert [k["scriptToken"] for k in kept] == ["1"] and stats["collisions"] == 1


# -- the drop ------------------------------------------------------------

def test_two_zips_are_refused(tree):
    write_zip(tree, [row()], name="a.zip")
    write_zip(tree, [row()], name="b.ZIP")
    with pytest.raises(mc.DropError, match="leave exactly one"):
        mc.find_zip(AS_OF)


def test_a_zip_without_the_scrips_file_is_refused(tree):
    zip_path = write_zip(tree, [row()], member="SOMETHING_ELSE.bcp")
    with pytest.raises(mc.DropError, match="no MCXScrips.bcp"):
        mc.read_member(zip_path)


def test_no_folder_means_no_input(tree):
    assert mc.find_zip(AS_OF) is None


# -- the step ------------------------------------------------------------

def test_the_step_writes_mcxs_own_token_as_counter_token_v2(tree, mcx_feed):
    write_zip(tree, [row(), future(token="582365", expiry=dt.date(2027, 8, 5)), spot()])
    mc.run(opts())
    out = pq.read_table(paths.normalized_dir(AS_OF) / mc.OUTPUT).to_pylist()
    assert [(r["script"], r["counterTokenV2"]) for r in out] == [
        ("GOLD26OCT166000CE", "513461"),
        ("GOLD27AUGFUT", "582365"),
        ("GOLD", "114"),
    ]
    assert [r["counterToken"] for r in out] == ["1", "2", "3"]


def test_a_dry_run_writes_nothing(tree, mcx_feed):
    write_zip(tree, [row()])
    mc.run(opts(dry_run=True))
    assert not (paths.normalized_dir(AS_OF) / mc.OUTPUT).exists()


def test_the_step_does_nothing_when_fyers_owns_ximc(tree, tmp_path, monkeypatch):
    ini = tmp_path / "config.ini"
    ini.write_text("[EXCHANGE:XIMC]\nfeed = fyers\nenabled = 1\nvenue_id = 20\n")
    monkeypatch.setenv("PREMARKET_CONFIG", str(ini))
    write_zip(tree, [row()])
    mc.run(opts())
    assert not (paths.normalized_dir(AS_OF) / mc.OUTPUT).exists()


def test_paths_names_the_same_output():
    """paths.FEED_OUTPUTS restates OUTPUT to keep paths.py free of pipeline
    imports; baskets resolve XIMC's file through it."""
    assert paths.FEED_OUTPUTS["XIMC"]["mcx"] == mc.OUTPUT
