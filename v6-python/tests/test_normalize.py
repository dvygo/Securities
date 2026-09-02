"""Unit tests for normalization modules."""
import configparser
import json
import contextlib
import os
import pathlib
import tempfile
from datetime import date, datetime, timezone

import pytest

from premarketv6 import cli, config, paths, runner
from premarketv6.sources import databento_src
from premarketv6.normalize import counter_token
from premarketv6.qa import lineage, report as qa_report, tokens as counter_token_qa
from premarketv6.normalize import broker_script, databento_norm, fields, price, session
from premarketv6.plugin import build as plugin
from premarketv6.plugin import postgres as plugin_pg
from premarketv6.sources import fyers_src


class TestPriceScaling:
    """Test price scaling functions."""

    def test_scale_price_default(self):
        """Test scaling with default India scale (feed is quoted in paise: 1 rupee = 100 units)."""
        assert price.scale_price(1.5) == 150

    def test_scale_price_zero(self):
        """Test scaling zero."""
        assert price.scale_price(0) == 0

    def test_scale_price_negative(self):
        """Test scaling negative prices."""
        assert price.scale_price(-1.0) == -100

    def test_scale_price_string(self):
        """Test scaling from string input."""
        assert price.scale_price("2.5") == 250

    def test_scale_price_invalid_string(self):
        """Test scaling invalid string."""
        assert price.scale_price("invalid") == 0


class TestSessionConversion:
    """Test session time conversion."""

    def test_ist_to_utc_morning(self):
        """Test IST to UTC conversion for morning time."""
        # 9:00 IST = 3:30 UTC
        result = session.ist_hhmm_to_utc("09:00")
        assert result in ["03:30", "03:31"]  # Allow for rounding

    def test_trading_session_ist_to_utc(self):
        """Test full session range conversion."""
        result = session.trading_session_ist_to_utc("0930-1530")
        assert result  # Should return non-empty string


class TestFyersAppendix:
    """Test Fyers appendix parsing."""

    def test_parse_fy_token_valid(self):
        """Test parsing valid fyToken."""
        # Format: EE SS YYMMDD EXTOKEN (appendix codes: NSE=10, CM=10)
        token = "101026070912345"
        result = fyers_src.parse_fy_token(token)
        assert result.get("exchange") == "NSE"
        assert result.get("segment") == "CM"

    def test_parse_fy_token_invalid(self):
        """Test parsing invalid fyToken."""
        result = fyers_src.parse_fy_token("invalid")
        assert result == {}

    def test_resolve_exchange_mic(self):
        """Test exchange/segment appendix codes to MIC mapping."""
        assert fyers_src.resolve_exchange_mic("10", "10") == "XNSE"
        assert fyers_src.resolve_exchange_mic("10", "11") == "XNFO"
        assert fyers_src.resolve_exchange_mic("12", "10") == "XBSE"

    def test_is_cash_instrument(self):
        """Test cash instrument classification."""
        assert fyers_src.is_cash_instrument("EQ")
        assert fyers_src.is_cash_instrument("MUTUALFUND")
        assert not fyers_src.is_cash_instrument("FUTSTK")

    def test_is_future(self):
        """Test futures classification."""
        assert fyers_src.is_future("FUTSTK")
        assert fyers_src.is_future("FUTIDX")
        assert not fyers_src.is_future("EQ")

    def test_is_option(self):
        """Test options classification."""
        assert fyers_src.is_option("OPTSTK")
        assert fyers_src.is_option("OPTIDX")
        assert not fyers_src.is_option("EQ")


class TestDatabentoParsing:
    """Test Databento symbol parsing."""

    def test_parse_occ_symbol_valid_call(self):
        """Test parsing valid OCC call symbol."""
        # SPX 240119 call at 5000: SPX 240119C00500000
        result = databento_norm.parse_occ_symbol("SPX240119C00500000")
        assert result.get("underlying") == "SPX"
        assert result.get("option_type") == "CALL"
        assert result.get("strike") == 500.0

    def test_parse_occ_symbol_valid_put(self):
        """Test parsing valid OCC put symbol."""
        result = databento_norm.parse_occ_symbol("SPX240119P00500000")
        assert result.get("option_type") == "PUT"

    def test_parse_occ_symbol_invalid(self):
        """Test parsing invalid OCC symbol."""
        result = databento_norm.parse_occ_symbol("INVALID")
        assert result == {}

    def test_underlying_root_from_symbol(self):
        """Test extracting underlying root from symbol."""
        assert databento_norm.underlying_root_from_stype_in("ES") == "ES"
        assert databento_norm.underlying_root_from_stype_in(".SPX") == "SPX"


class TestVenueTokenPrefix:
    """scriptToken is the bare Databento instrument_id (databento_norm.prefixed_token).

    Venue prefixing (XNAS 111 / XCBO 222 / XCME 333) was removed by request. These
    tests are inverted from their original form on purpose: they now pin the
    unprefixed contract, and in particular pin the collision it reintroduces, so
    that behaviour is asserted rather than discovered in production.
    """

    def test_token_is_the_bare_instrument_id(self):
        assert databento_norm.prefixed_token("XNAS", 38) == "38"
        assert databento_norm.prefixed_token("XCBO", 637543226) == "637543226"
        assert databento_norm.prefixed_token("XCME", 2544437) == "2544437"

    def test_result_is_still_all_digits(self):
        """A token must keep passing an is-numeric test."""
        assert databento_norm.prefixed_token("XCBO", 637543226).isdigit()

    def test_pandas_float_widening_is_stripped(self):
        """pandas renders an int column as float when any value is missing.

        Unrelated to prefixing, so it survives the removal.
        """
        assert databento_norm.prefixed_token("XNAS", "38.0") == "38"

    def test_unknown_venue_passes_through(self):
        assert databento_norm.prefixed_token("XNSE", 12345) == "12345"

    def test_non_numeric_id_passes_through(self):
        assert databento_norm.prefixed_token("XNAS", "not-an-id") == "not-an-id"

    def test_tokens_fit_int32_again(self):
        """Without a prefix, ids are back inside a 32-bit field."""
        token = int(databento_norm.prefixed_token("XCBO", 1509950237))
        assert token == 1509950237
        assert token < 2**31 - 1

    def test_venues_now_collide_for_a_shared_instrument_id(self):
        """Documents the cost of removing the prefix.

        Databento only guarantees instrument_id is unique within a dataset, so the
        same id on three venues now yields one token, not three. Anything keying on
        (token, trade_date) without an exchange column -- which is exactly the pg
        symbol-master table plugin/postgres.py appends to -- can collide.
        """
        ids = {databento_norm.prefixed_token(v, 12345) for v in ("XNAS", "XCBO", "XCME")}
        assert ids == {"12345"}

    def test_mappers_emit_bare_tokens(self):
        common = {"stype_out": "instrument_id"}
        xnas = databento_norm.map_xnas_row({**common, "stype_in_symbol": "AAPL", "instrument_id": 38}, date(2026, 8, 3))
        xcbo = databento_norm.map_xcbo_row(
            {**common, "stype_in_symbol": "META  260918C00705000", "instrument_id": 637543226}, date(2026, 8, 3))
        xcme = databento_norm.map_xcme_row({**common, "stype_in_symbol": "ESZ6", "instrument_id": 10252}, date(2026, 8, 3))
        assert xnas["scriptToken"] == "38"
        assert xcbo["scriptToken"] == "637543226"
        assert xcme["scriptToken"] == "10252"


class TestBrokerScript:
    """brokerScript1 derivation (normalize/broker_script.py)."""

    # 2026-09-01 and 2026-08-01 UTC, the resolved expirations for a *U6 / *Q6 contract.
    EXP_2026_09 = 1788220800 * 10**9
    EXP_2026_08 = 1785542400 * 10**9

    def test_equity_is_exact_copy_of_script(self):
        assert broker_script.from_equity("AAPL") == "AAPL"

    def test_glbx_future(self):
        assert broker_script.from_glbx("ESU6", self.EXP_2026_09) == "ES/U26"

    def test_glbx_option(self):
        assert broker_script.from_glbx("E1AQ6 C7545", self.EXP_2026_08) == "E1A/Q26/7545C"

    def test_occ_option_keeps_fractional_strike(self):
        symbol = "AAPL  260803C00302500"
        parsed = databento_norm.parse_occ_symbol(symbol)
        assert broker_script.from_occ(symbol, parsed) == "AAPL/260803/302.5C"

    def test_occ_option_drops_trailing_zeros(self):
        symbol = "META  260918C00705000"
        parsed = databento_norm.parse_occ_symbol(symbol)
        assert broker_script.from_occ(symbol, parsed) == "META/260918/705C"

    def test_occ_put(self):
        symbol = "META  271217P00650000"
        parsed = databento_norm.parse_occ_symbol(symbol)
        assert broker_script.from_occ(symbol, parsed) == "META/271217/650P"

    def test_year_comes_from_expiration_not_the_symbol_digit(self):
        """ESZ0 is 2030, not 2020 -- the single digit alone is ambiguous, so the
        decade must come from the already-resolved expiration column."""
        exp_2030 = 1922486400 * 10**9  # 2030-12-01 UTC
        assert broker_script.from_glbx("ESZ0", exp_2030) == "ES/Z30"

    def test_calendar_spread_copies_script(self):
        """Without the combo guard this parses as root "ESH7-ES" + Z + 7."""
        assert broker_script.from_glbx("ESH7-ESZ7", self.EXP_2026_09) == "ESH7-ESZ7"

    def test_exchange_defined_combo_copies_script(self):
        assert broker_script.from_glbx("UD:1V: GN 2533155", 0) == "UD:1V: GN 2533155"

    def test_missing_expiration_copies_script(self):
        """Decade is unresolvable without an expiration, so do not guess."""
        assert broker_script.from_glbx("ESZ6", 0) == "ESZ6"

    def test_unparseable_occ_copies_script(self):
        assert broker_script.from_occ("NOT_AN_OCC_SYMBOL", {}) == "NOT_AN_OCC_SYMBOL"

    def test_reserved_columns_are_blank(self):
        result = broker_script.fill_unspecified({})
        assert result == {"brokerScript2": "", "brokerScript3": "", "brokerScript4": ""}

    def test_all_four_columns_present_in_canonical_schema(self):
        for col in broker_script.BROKER_SCRIPT_COLUMNS:
            assert col in paths.NORMALIZED_COLUMNS

    def test_xnas_mapper_populates_broker_script1(self):
        row = {"stype_in_symbol": "AAPL", "stype_out": "instrument_id", "instrument_id": 38}
        result = databento_norm.map_xnas_row(row, date(2026, 8, 3))
        assert result["brokerScript1"] == "AAPL"
        assert result["brokerScript2"] == ""

    def test_xcbo_mapper_populates_broker_script1(self):
        row = {
            "stype_in_symbol": "META  260918C00705000",
            "stype_out": "instrument_id",
            "instrument_id": 637543226,
        }
        result = databento_norm.map_xcbo_row(row, date(2026, 8, 3))
        assert result["brokerScript1"] == "META/260918/705C"

    def test_xcme_mapper_broker_script1_agrees_with_expiration(self):
        row = {"stype_in_symbol": "ESZ6", "stype_out": "instrument_id", "instrument_id": 10252}
        result = databento_norm.map_xcme_row(row, date(2026, 8, 3))
        year = datetime.fromtimestamp(result["expiration"] / 1e9, tz=timezone.utc).year
        assert result["brokerScript1"] == f"ES/Z{year % 100:02d}"


class TestPluginExpiry:
    """Plugin expiry: which contracts are stripped, and what expirydate they carry."""

    CUTOFF = plugin._cutoff_ns("20260826")
    DAY_NS = 86_400 * 10 ** 9
    UNDEF = "18446744073709551615"   # Databento's unset-timestamp sentinel

    def test_cutoff_is_midnight_utc_on_the_trade_date(self):
        assert self.CUTOFF == int(
            datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp()
        ) * 10 ** 9

    def test_undef_timestamp_reads_as_never_expires(self):
        """UINT64_MAX is "unset", not the year 586524.

        Every XNAS equity and every OPRA SPOT leg arrives this way. Read
        literally it survives any expiry test and lands in expirydate as
        18446744073.
        """
        row = {"expiration": "0", "def_expiration": self.UNDEF}
        assert plugin._expiry_ns(row) == 0
        assert plugin._expiry_seconds(row) == 0
        assert plugin._is_expired(row, self.CUTOFF) is False

    def test_undef_survives_the_int_parse_exactly(self):
        """float() cannot hold UINT64_MAX, so it must not be parsed through float."""
        assert session.as_ns(self.UNDEF) == 0
        assert int(float(self.UNDEF)) != int(self.UNDEF)

    def test_zero_expiration_is_kept_not_stripped(self):
        """A 0 means never expires. Treating it as long-expired deletes every equity."""
        row = {"expiration": "0", "def_expiration": ""}
        assert plugin._is_expired(row, self.CUTOFF) is False

    def test_expired_before_trade_date_is_stripped(self):
        row = {"expiration": str(self.CUTOFF - self.DAY_NS)}
        assert plugin._is_expired(row, self.CUTOFF) is True

    def test_expiring_on_the_trade_date_is_kept(self):
        """0DTE contracts are live during the session the snapshot describes."""
        row = {"expiration": str(self.CUTOFF + 20 * 3600 * 10 ** 9)}
        assert plugin._is_expired(row, self.CUTOFF) is False

    def test_def_expiration_used_when_canonical_expiration_is_zero(self):
        """XCME carries real OPTION/FUTURE rows whose canonical expiration is 0.

        Without this fallback they read as perpetual and never age out.
        """
        row = {"expiration": "0", "def_expiration": str(self.CUTOFF - self.DAY_NS)}
        assert plugin._is_expired(row, self.CUTOFF) is True

    def test_def_expiration_wins_over_canonical_expiration(self):
        """The venue's own expiry beats the symbol-derived one.

        XCME's canonical expiration is regexed off the symbol's month code with
        the day hardcoded to the 1st, so it lands before the real expiry and
        strips contracts that are still live.
        """
        row = {
            "expiration": str(self.CUTOFF - self.DAY_NS),      # "1st of month", already past
            "def_expiration": str(self.CUTOFF + 20 * self.DAY_NS),  # real expiry, still ahead
        }
        assert plugin._is_expired(row, self.CUTOFF) is False

    def test_canonical_expiration_used_when_venue_gives_nothing(self):
        """Fyers venues carry no def_expiration at all."""
        row = {"expiration": str(self.CUTOFF - self.DAY_NS), "def_expiration": ""}
        assert plugin._is_expired(row, self.CUTOFF) is True

    def test_float_rendered_timestamp_still_parses(self):
        """pandas widens an int column to float when any value is missing."""
        assert session.as_ns("1787616000000000000.0") == 1_787_616_000_000_000_000

    def test_unparseable_and_negative_are_never_expired(self):
        """Fail safe: keep the row rather than silently deleting it."""
        for bad in ("abc", "", None, "-5"):
            assert plugin._is_expired({"expiration": bad}, self.CUTOFF) is False


class TestGlbxContractMonth:
    """GLBX single-digit years are ambiguous across decades; how that is resolved."""

    REF = date(2026, 8, 26)

    @staticmethod
    def _ns(y, m, d):
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp()) * 10 ** 9

    def _month(self, base, anchor_ns=0):
        ns = databento_norm.glbx_expiration_ns(base, self.REF, anchor_ns=anchor_ns)
        if not ns:
            return None
        dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
        return (dt.year, dt.month)

    def test_anchor_resolves_decade_from_the_venue_expiry(self):
        """0NGF1 is the January 2031 contract; it stops trading 2030-12-28.

        Guessing the decade from the run date put it in 2021, ten years early,
        and brokerScript1 inherited the wrong year.
        """
        assert self._month("0NGF1", self._ns(2030, 12, 28)) == (2031, 1)

    def test_anchor_keeps_a_december_contract_in_its_own_year(self):
        """0BZ0 expires 2031-01-01 but is the Z30 contract, not Z31.

        The contract month must not follow the expiry into the next year.
        """
        assert self._month("0BZ0", self._ns(2031, 1, 1)) == (2030, 12)

    def test_anchor_leaves_a_current_contract_alone(self):
        assert self._month("ESZ6", self._ns(2026, 12, 18)) == (2026, 12)

    def test_unanchored_fallback_rolls_a_past_year_forward(self):
        """Basket rows carry no definition record, so the run date is all there is."""
        assert self._month("0NGF1") == (2031, 1)

    def test_unanchored_fallback_allows_one_year_back(self):
        """A just-expired contract stays in the year it belongs to."""
        assert self._month("ESZ5") == (2025, 12)

    def test_unanchored_fallback_resolves_the_wrap_digit(self):
        assert self._month("ESZ0") == (2030, 12)

    def test_symbol_with_no_month_code_returns_zero(self):
        assert databento_norm.glbx_expiration_ns("ES", self.REF) == 0

    def test_xcme_row_expiration_is_the_venue_value_not_the_symbol(self):
        """map_xcme_row must prefer the definition record's own expiration."""
        real = self._ns(2026, 12, 18)
        row = {
            "stype_in_symbol": "ESZ6",
            "stype_out": "instrument_id",
            "instrument_id": 10252,
            "expiration": str(real),
        }
        result = databento_norm.map_xcme_row(row, self.REF)
        assert result["expiration"] == real
        # brokerScript1 still names the contract month, not the expiry date.
        assert result["brokerScript1"] == "ES/Z26"

    def test_xcme_row_falls_back_when_no_definition_expiration(self):
        row = {"stype_in_symbol": "ESZ6", "stype_out": "instrument_id", "instrument_id": 10252}
        result = databento_norm.map_xcme_row(row, self.REF)
        assert result["expiration"] == self._ns(2026, 12, 1)  # contract month, 1st
        assert result["brokerScript1"] == "ES/Z26"


class TestExchangeEnabledFlag:
    """[EXCHANGE:*] enabled is a strict 0/1 flag, not a boolean spelling."""

    @staticmethod
    def _section(body: str):
        cfg = configparser.ConfigParser()
        cfg.read_string(f"[EXCHANGE:XNAS]\n{body}\n")
        return cfg["EXCHANGE:XNAS"]

    def test_one_enables_and_zero_disables(self):
        assert config._flag_01(self._section("enabled = 1"), "EXCHANGE:XNAS") is True
        assert config._flag_01(self._section("enabled = 0"), "EXCHANGE:XNAS") is False

    def test_absent_means_enabled(self):
        """A section written before this knob existed keeps running."""
        assert config._flag_01(self._section("feed = databento"), "EXCHANGE:XNAS") is True

    def test_surrounding_whitespace_is_tolerated(self):
        assert config._flag_01(self._section("enabled =  1  "), "EXCHANGE:XNAS") is True

    @pytest.mark.parametrize("value", ["true", "false", "yes", "no", "on", "off", "", "2", "-1"])
    def test_boolean_spellings_are_rejected(self, value):
        """One spelling only: getboolean would quietly accept all of these.

        Rejected rather than defaulted because both defaults are wrong -- a
        silent disable loses a day's data, a silent enable pushes rows nobody
        asked for.
        """
        with pytest.raises(ValueError, match="must be 0 or 1"):
            config._flag_01(self._section(f"enabled = {value}"), "EXCHANGE:XNAS")

    def test_error_names_the_venue(self):
        with pytest.raises(ValueError, match=r"\[EXCHANGE:XCBO\]"):
            config._flag_01(self._section("enabled = true"), "EXCHANGE:XCBO")


class TestVenueSelection:
    """--venue narrows the per-venue steps; an unknown MIC must not pass quietly."""

    @staticmethod
    def _opts(venues=()):
        return runner.Opts(as_of="20260826", date_dir="20260826", venues=venues)

    def test_no_selection_means_every_venue(self):
        opts = self._opts()
        assert runner.venue_selected(opts, "XNAS")
        assert runner.venue_selected(opts, "XCME")

    def test_selection_admits_only_the_named(self):
        opts = self._opts(("XNAS",))
        assert runner.venue_selected(opts, "XNAS")
        assert not runner.venue_selected(opts, "XCME")

    def test_match_is_case_insensitive(self):
        assert runner.venue_selected(self._opts(("XNAS",)), "xnas")

    def test_values_are_uppercased_and_deduped(self):
        assert cli._venue_selection(["xnas", "XNAS"]) == ("XNAS",)

    def test_comma_separated_and_repeated_both_work(self):
        assert cli._venue_selection(["XNAS,XCME"]) == ("XNAS", "XCME")
        assert cli._venue_selection(["XNAS", "XCME"]) == ("XNAS", "XCME")

    def test_empty_selection_is_empty_tuple(self):
        assert cli._venue_selection([]) == ()

    def test_unknown_venue_exits_rather_than_matching_nothing(self):
        """A typo would otherwise produce a successful run that wrote nothing.

        That is indistinguishable from a day whose data never arrived, so it
        has to fail at the argument instead.
        """
        with pytest.raises(SystemExit, match="Unknown venue"):
            cli._venue_selection(["XNSA"])

    def test_error_lists_what_is_configured(self):
        with pytest.raises(SystemExit, match="XNAS"):
            cli._venue_selection(["NOPE"])


class TestBasketsToggle:
    """[baskets].enabled drives the step. There is no flag for it."""

    @staticmethod
    @contextlib.contextmanager
    def _config(body: str):
        """Point the whole pipeline at a throwaway config.ini for one block."""
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "config.ini"
            path.write_text(body)
            old = os.environ.get("PREMARKET_CONFIG")
            os.environ["PREMARKET_CONFIG"] = str(path)
            try:
                yield
            finally:
                if old is None:
                    os.environ.pop("PREMARKET_CONFIG", None)
                else:
                    os.environ["PREMARKET_CONFIG"] = old

    def test_absent_section_means_enabled(self):
        with self._config("[paths]\ndata_dir = x\n"):
            assert config.load_baskets().enabled
            assert "baskets" in [s.name for s in runner.build_normalizer_steps([])]

    def test_enabled_one_keeps_the_step(self):
        with self._config("[baskets]\nenabled = 1\n"):
            assert "baskets" in [s.name for s in runner.build_normalizer_steps([])]

    def test_enabled_zero_removes_the_step(self):
        with self._config("[baskets]\nenabled = 0\n"):
            names = [s.name for s in runner.build_normalizer_steps([])]
            assert "baskets" not in names
            assert "csv-export" in names          # steps around it are untouched
            assert "normalize-databento" in names

    def test_config_beats_naming_baskets_in_only(self):
        """--only cannot turn a step back on that the config switched off."""
        with self._config("[baskets]\nenabled = 0\n"):
            assert runner.build_normalizer_steps(["baskets"]) == []

    def test_baskets_rejects_boolean_spellings_too(self):
        with self._config("[baskets]\nenabled = true\n"):
            with pytest.raises(ValueError, match=r"\[baskets\] enabled must be 0 or 1"):
                config.load_baskets()

    def test_there_is_no_baskets_cli_flag(self):
        """The knob is config-only; a --no-baskets would reintroduce per-run drift."""
        parser = cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["normalize", "--no-baskets"])


class TestPluginTableDDL:
    """The appender's CREATE TABLE, and the gate that keeps it off by default."""

    def test_ddl_covers_every_plugin_column_in_order(self):
        sql = plugin_pg._create_table_sql("public", "contracts")
        positions = [sql.index(f'"{c}"') for c in plugin.PLUGIN_COLUMNS]
        assert positions == sorted(positions), "DDL column order must match PLUGIN_COLUMNS"

    def test_ddl_is_if_not_exists_so_it_never_alters_a_real_table(self):
        sql = plugin_pg._create_table_sql("public", "contracts")
        assert "CREATE TABLE IF NOT EXISTS" in sql

    def test_ddl_carries_the_documented_primary_key(self):
        sql = plugin_pg._create_table_sql("public", "contracts")
        assert 'PRIMARY KEY ("token", "trade_date")' in sql

    def test_a_plugin_column_with_no_type_raises(self):
        """Adding a column to PLUGIN_COLUMNS must not silently produce a table missing it."""
        original = plugin.PLUGIN_COLUMNS[:]
        try:
            plugin.PLUGIN_COLUMNS.append("brand_new_column")
            with pytest.raises(ValueError, match="brand_new_column"):
                plugin_pg._create_table_sql("public", "contracts")
        finally:
            plugin.PLUGIN_COLUMNS[:] = original

    def test_create_table_defaults_off(self):
        """The default is load-bearing: it is what turns a mistyped table into an error."""
        cfg = configparser.ConfigParser()
        cfg.read_string("[postgres-plugin]\nschema = public\ntable = contracts\n")
        assert config._flag_01(cfg["postgres-plugin"], "postgres-plugin",
                               "create_table", "0") is False

    def test_create_table_one_enables(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[postgres-plugin]\ncreate_table = 1\n")
        assert config._flag_01(cfg["postgres-plugin"], "postgres-plugin",
                               "create_table", "0") is True

    def test_create_table_rejects_boolean_spellings(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[postgres-plugin]\ncreate_table = true\n")
        with pytest.raises(ValueError, match="create_table must be 0 or 1"):
            config._flag_01(cfg["postgres-plugin"], "postgres-plugin", "create_table", "0")


class TestPluginUpsertSQL:
    """The push overwrites on (token, trade_date) instead of appending."""

    def _sql(self):
        return plugin_pg._upsert_sql("public", "resultset", plugin.PLUGIN_COLUMNS)

    def test_conflict_target_is_the_primary_key(self):
        assert 'ON CONFLICT ("token", "trade_date")' in self._sql()

    def test_it_updates_rather_than_ignoring(self):
        """DO NOTHING would leave yesterday's values in place; ours must win."""
        sql = self._sql()
        assert "DO UPDATE SET" in sql
        assert "DO NOTHING" not in sql

    def test_every_non_key_column_is_overwritten(self):
        sql = self._sql()
        for col in plugin.PLUGIN_COLUMNS:
            if col in plugin_pg.PLUGIN_PRIMARY_KEY:
                continue
            assert f'"{col}" = EXCLUDED."{col}"' in sql, col

    def test_key_columns_are_not_in_the_set_list(self):
        """Assigning the matched key is a no-op Postgres rejects."""
        set_clause = self._sql().split("DO UPDATE SET", 1)[1]
        for col in plugin_pg.PLUGIN_PRIMARY_KEY:
            assert f'"{col}" = EXCLUDED' not in set_clause, col

    def test_distinct_on_guards_duplicates_within_one_push(self):
        """Without it Postgres raises 'cannot affect row a second time'."""
        assert 'SELECT DISTINCT ON ("token", "trade_date")' in self._sql()

    def test_it_reads_from_the_staging_table(self):
        assert plugin_pg._TEMP_TABLE in self._sql()

    def test_all_key_columns_would_raise(self):
        with pytest.raises(ValueError, match="nothing to update"):
            plugin_pg._upsert_sql("public", "t", list(plugin_pg.PLUGIN_PRIMARY_KEY))


class TestPluginNullSafety:
    """No plugin column is ever empty: the pushed table must hold no NULL."""

    @staticmethod
    def _empty(row):
        return [k for k, v in row.items()
                if v is None or (isinstance(v, str) and not v.strip())]

    def test_an_entirely_empty_input_still_fills_every_column(self):
        """The worst case: a row carrying nothing the mapper recognises."""
        row = plugin.map_row({}, "2026-08-27", "XCME")
        assert self._empty(row) == []
        assert set(row) == set(plugin.PLUGIN_COLUMNS)

    def test_columns_the_canonical_schema_never_carries_are_filled(self):
        """lotmultiple/freeze_qty have no source at all; ticksize has none for XCME."""
        row = plugin.map_row({}, "2026-08-27", "XCME")
        for col in ("lotmultiple", "freeze_qty", "ticksize"):
            assert row[col] == plugin.NULL_FILL

    def test_real_values_are_not_overwritten(self):
        row = plugin.map_row(
            {"script": "ESZ6", "scriptToken": "9", "counterTokenV2": "130",
             "scriptInstrumentType": "FUTIDX", "scriptInstrumentType2": "FUTURE",
             "underlying_root": "ES", "lotSize": "5", "tickSize": "25",
             "multiplier": "1000000000"},
            "2026-08-27", "XCME")
        assert row["token"] == "130"
        assert row["name"] == "ESZ6"
        assert row["lotsize"] == "5"
        assert row["ticksize"] == "25"
        assert row["series"] == "XX"

    def test_whitespace_only_counts_as_empty(self):
        """A value of " " reaches Postgres as a non-NULL no more useful than a NULL."""
        row = plugin.map_row({"script": "   ", "scriptToken": "1"}, "2026-08-27", "XCME")
        assert row["name"] == plugin.NULL_FILL

    def test_zero_survives_where_it_is_a_real_value(self):
        """expirydate 0 means "never expires" and must not be substituted."""
        row = plugin.map_row({"scriptToken": "1", "expiration": 0}, "2026-08-27", "XCME")
        assert row["expirydate"] == "0"

    def test_strikeprice_zero_becomes_minus_one(self):
        """0 is the no-strike value here, and reads as a plausible strike if left."""
        row = plugin.map_row({"scriptToken": "1", "strike": 0}, "2026-08-27", "XCME")
        assert row["strikeprice"] == "-1"

    def test_a_real_strike_is_untouched(self):
        row = plugin.map_row({"scriptToken": "1", "strike": "665000000000"},
                             "2026-08-27", "XCME")
        assert row["strikeprice"] == "665000000000"


class TestPluginColumnFills:
    """Absent values get their column's own placeholder, not a generic one."""

    def test_missing_optiontype_is_xx(self):
        """XX is what `series` already uses for a non-option; no second marker."""
        row = plugin.map_row({"scriptToken": "1", "scriptInstrumentType2": "FUTURE"},
                             "2026-08-27", "XCME")
        assert row["optiontype"] == "XX"

    def test_real_optiontype_survives(self):
        row = plugin.map_row({"scriptToken": "1", "optionType": "CALL"}, "2026-08-27", "XCBO")
        assert row["optiontype"] == "CE"

    def test_blank_segment_becomes_fno(self):
        """Spread types are absent from SEGMENT_BY_TYPE2 and are derivatives."""
        row = plugin.map_row({"scriptToken": "1", "scriptInstrumentType2": "FUTURE_SPREAD"},
                             "2026-08-27", "XCME")
        assert row["segment"] == "F&O"

    def test_equity_segment_stays_cm(self):
        """The fill must not relabel cash-market instruments as derivatives."""
        row = plugin.map_row({"scriptToken": "1", "scriptInstrumentType2": "EQUITY"},
                             "2026-08-27", "XNAS")
        assert row["segment"] == "CM"

    def test_ticksize_comes_from_the_definition_record(self):
        """Canonical tickSize is blank for every Databento venue."""
        row = plugin.map_row(
            {"scriptToken": "1", "tickSize": "", "def_min_price_increment": "25000000"},
            "2026-08-27", "XCME")
        assert row["ticksize"] == "25000000"

    def test_canonical_ticksize_wins_when_present(self):
        """Fyers venues carry their own; the definition fallback must not override."""
        row = plugin.map_row(
            {"scriptToken": "1", "tickSize": "500", "def_min_price_increment": "25000000"},
            "2026-08-27", "XNSE")
        assert row["ticksize"] == "500"

    def test_ticksize_sentinel_falls_through_to_the_placeholder(self):
        """def_min_price_increment carries INT64/UINT64 sentinels for "no increment"."""
        row = plugin.map_row(
            {"scriptToken": "1", "tickSize": "", "def_min_price_increment": str(2**63 - 1)},
            "2026-08-27", "XCME")
        assert row["ticksize"] == plugin.NULL_FILL

    def test_every_declared_column_is_present(self):
        """A column added to PLUGIN_COLUMNS is filled too, not silently absent."""
        row = plugin.map_row({}, "2026-08-27", "XNSE")
        for col in plugin.PLUGIN_COLUMNS:
            assert col in row, col


class TestBackfillDates:
    """--dates: one batch job per date, submitted up front."""

    def test_parses_a_comma_separated_list(self):
        assert cli._date_list("20260827,20260825,20260101") == ("20260827", "20260825", "20260101")

    def test_orders_newest_first(self):
        """The readiness check compares against the prior session, so an
        unpublished newest date should fail before older jobs are submitted."""
        assert cli._date_list("20260101,20260827,20260825")[0] == "20260827"

    def test_tolerates_whitespace_and_dedupes(self):
        assert cli._date_list(" 20260827 , 20260827 ") == ("20260827",)

    def test_rejects_a_non_yyyymmdd_date(self):
        """Silently dropping it would submit fewer jobs than the user listed."""
        with pytest.raises(SystemExit, match="not a YYYYMMDD date"):
            cli._date_list("2026-08-27")

    def test_rejects_an_impossible_date(self):
        with pytest.raises(SystemExit, match="not a YYYYMMDD date"):
            cli._date_list("20260231")

    def test_rejects_an_empty_list(self):
        with pytest.raises(SystemExit, match="contained no dates"):
            cli._date_list(" , , ")

    def test_dates_flag_exists_on_a_venue_parser(self):
        args = cli.create_parser().parse_args(["xcbo", "--all-symbols", "--dates=20260827"])
        assert args.dates == "20260827"
        assert args.all_symbols is True

    def test_today_flag_exists_and_defaults_off(self):
        args = cli.create_parser().parse_args(["xcbo", "--all-symbols"])
        assert args.today is False
        assert cli.create_parser().parse_args(["xcbo", "--today"]).today is True

    def test_a_date_that_already_has_a_file_submits_nothing(self, tmp_path, monkeypatch):
        """A present file is an operator drop or a prior success -- refetching is waste."""
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        venue_cfg = config.load_exchanges()["xcbo"]
        have = paths.manual_venue_dir("20260825", "XCBO")
        have.mkdir(parents=True)
        (have / "opra.definition.dbn.zst").write_bytes(b"x")

        submitted = []

        class FakeClient:
            class batch:
                @staticmethod
                def submit_job(**kw):
                    submitted.append(kw)
                    return {"id": "J", "state": "received"}

        written = databento_src.download_definitions_for_dates(
            FakeClient(), venue_cfg, "raw_symbol", ("20260825",))
        assert submitted == []
        assert written == {}


class TestCounterTokenV2Numbering:
    """One shared sequence starting at 1 (normalize/counter_token.py).

    The prefix scheme these tests used to pin is gone. A token is now just the
    sequence number, so the properties worth pinning moved: not "does the
    prefix survive widening" but "can two venues ever be handed the same
    number", which is what the pg key (token, trade_date) depends on.
    """

    def test_the_first_token_is_one(self):
        assert counter_token.assign(counter_token.FIRST_TOKEN) == "1"

    def test_the_token_is_the_sequence_number(self):
        assert counter_token.assign(42) == "42"
        assert counter_token.assign(2_006_525) == "2006525"

    def test_zero_is_not_issuable(self):
        """A zero token is indistinguishable from an unset integer downstream."""
        with pytest.raises(ValueError):
            counter_token.assign(0)

    def test_it_refuses_to_cross_int32(self):
        assert counter_token.assign(counter_token.INT32_MAX) == str(counter_token.INT32_MAX)
        with pytest.raises(ValueError, match="exhausted"):
            counter_token.assign(counter_token.INT32_MAX + 1)

    def test_a_sequence_hands_out_consecutive_numbers(self):
        seq = counter_token.Sequence()
        assert [seq.take() for _ in range(3)] == [1, 2, 3]
        assert seq.issued == 3 and seq.drawn == 3

    def test_a_sequence_resumes_where_it_was_left(self):
        """The second run of a day must not reissue the first run's numbers."""
        seq = counter_token.Sequence(2_006_525)
        assert seq.take() == 2_006_526
        assert seq.drawn == 1

    def test_two_venues_cannot_be_handed_the_same_number(self):
        """The property the venue prefix used to provide.

        Now it comes from both venues drawing from one counter, which is the
        only thing standing between the plugin push and a duplicate key on
        (token, trade_date).
        """
        seq = counter_token.Sequence()
        a = counter_token.carry_forward(None, ["A", "B", "C"], 10, seq)
        b = counter_token.carry_forward(None, ["A", "B", "C"], 12, seq)
        assert set(a.assigned.values()) & set(b.assigned.values()) == set()

    def test_the_sequence_refuses_to_wrap(self):
        seq = counter_token.Sequence(counter_token.INT32_MAX)
        with pytest.raises(ValueError, match="exhausted"):
            seq.take()


class TestCounterTokenV2Capacity:
    """int32 is now one budget for the whole estate, not a slice per prefix."""

    def test_the_whole_estate_shares_int32(self):
        assert counter_token.capacity() == counter_token.INT32_MAX

    def test_a_real_week_is_a_rounding_error_against_it(self):
        """Five days, six venues, ~3.0M numbers issued."""
        counter_token.check_capacity("XCBO", 3_000_000, 2_076_251)

    def test_it_refuses_a_day_that_would_cross_int32(self):
        with pytest.raises(ValueError, match="past int32"):
            counter_token.check_capacity("XCBO", counter_token.INT32_MAX - 10, 11)

    def test_the_error_names_the_venue_and_the_ceiling(self):
        with pytest.raises(ValueError, match=r"XCME.*2,147,483,647"):
            counter_token.check_capacity("XCME", counter_token.INT32_MAX, 1)


class TestCounterTokenV2Validate:
    """The config pre-flight. venue_id no longer reaches the token."""

    @staticmethod
    def _venues(**ids):
        return {name.lower(): config.ExchangeCfg(venue_name=name, venue_id=vid)
                for name, vid in ids.items()}

    def test_unique_ids_are_accepted(self):
        assert counter_token.validate(self._venues(XCBO=10, XCME=12, XNAS=14)) == {}

    def test_adjacent_ids_are_now_fine(self):
        """They were not, when a venue owned venue_id AND venue_id+1 as prefixes.

        Nothing is derived from venue_id any more, so ids only have to be
        unique.
        """
        assert counter_token.validate(self._venues(XCBO=10, XCME=11)) == {}

    def test_a_single_digit_id_is_now_fine(self):
        """The two-digit rule existed only to stop widening prefixes colliding."""
        assert counter_token.validate(self._venues(XCBO=1, XCME=2)) == {}

    def test_an_unset_venue_id_is_still_an_error(self):
        assert "unset" in counter_token.validate(self._venues(XCBO=0))["XCBO"][0]

    def test_a_duplicated_id_is_still_an_error(self):
        """A manifest has to be holdable against the venue it claims to describe."""
        assert "XCME" in counter_token.validate(self._venues(XCBO=10, XCME=10))

    def test_the_live_config_passes(self):
        assert counter_token.validate(config.load_exchanges()) == {}


class TestCounterTokenV2CarryForward:
    """carry_forward(): keep what stayed, recycle what left, extend for the rest."""

    def test_a_first_day_numbers_from_one(self):
        tokens = counter_token.carry_forward(None, ["B", "A", "C"], 10, counter_token.Sequence())
        assert tokens.assigned == {"A": 1, "B": 2, "C": 3}
        assert tokens.highest == 3

    def test_a_surviving_script_keeps_its_offset(self):
        """The whole point of v2: the number is joinable across dates."""
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, counter_token.Sequence())
        day2 = counter_token.carry_forward(day1, ["A", "B", "C"], 10, counter_token.Sequence())
        assert day2.assigned == day1.assigned

    def test_a_departed_scripts_token_is_recycled(self):
        """And this is exactly what makes a v2 token ambiguous across dates."""
        seq = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, seq)
        day2 = counter_token.carry_forward(day1, ["A", "C", "D"], 10, seq)
        assert day2.assigned["D"] == day1.assigned["B"]        # B's number, now D's
        assert seq.drawn == 3                                  # nothing new drawn

    def test_the_free_pool_is_drained_before_the_sequence_is_drawn_from(self):
        seq = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, seq)
        day2 = counter_token.carry_forward(day1, ["A", "D", "E", "F"], 10, seq)
        assert sorted(day2.assigned.values()) == [1, 2, 3, 4]
        assert seq.issued == 4          # only one number ever drawn beyond day 1

    def test_leftover_free_offsets_survive_to_the_next_day(self):
        day1 = counter_token.carry_forward(None, ["A", "B", "C", "D"], 10, counter_token.Sequence())
        day2 = counter_token.carry_forward(day1, ["A"], 10, counter_token.Sequence())
        assert day2.free == [2, 3, 4]
        day3 = counter_token.carry_forward(day2, ["A", "Z"], 10, counter_token.Sequence())
        assert day3.assigned["Z"] == 2 and day3.free == [3, 4]

    def test_free_and_assigned_never_overlap(self):
        """An offset in both pools would be handed out while still live."""
        day1 = counter_token.carry_forward(None, list("ABCDEF"), 10, counter_token.Sequence())
        day2 = counter_token.carry_forward(day1, list("ACEG"), 10, counter_token.Sequence())
        assert set(day2.free) & set(day2.assigned.values()) == set()

    def test_row_order_and_duplicates_do_not_change_the_result(self):
        """What makes a re-run byte-identical."""
        a = counter_token.carry_forward(None, ["C", "A", "B", "A"], 10, counter_token.Sequence())
        b = counter_token.carry_forward(None, ["B", "C", "A"], 10, counter_token.Sequence())
        assert a.assigned == b.assigned

    def test_blank_scripts_are_not_numbered(self):
        tokens = counter_token.carry_forward(None, ["A", "", None, "B"], 10, counter_token.Sequence())
        assert set(tokens.assigned) == {"A", "B"}

    def test_token_renders_as_the_plain_sequence_number(self):
        tokens = counter_token.carry_forward(None, ["A", "B"], 10, counter_token.Sequence())
        assert tokens.token("A") == "1"
        assert tokens.token("B") == "2"

    def test_an_unknown_script_gets_an_empty_token_not_a_wrong_one(self):
        """The validator's "populated" check is what turns this into a failure."""
        assert counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()).token("Z") == ""

    def test_a_backfilled_day_disagrees_with_the_day_after_it(self):
        """The failure v2 cannot avoid, pinned rather than left to be discovered.

        Day 3 is normalized while day 2 is missing, then day 2 is backfilled. Day
        2 chains from day 1 and reuses B's offset for D; day 3 chained from day 1
        too and gave that offset to E. One number, two instruments, on consecutive
        dates. That is the case the registry-issued column was built for.
        """
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, counter_token.Sequence())
        day3 = counter_token.carry_forward(day1, ["A", "C", "E"], 10, counter_token.Sequence())
        day2 = counter_token.carry_forward(day1, ["A", "C", "D"], 10, counter_token.Sequence())
        clash = {t for t in set(day2.assigned.values()) & set(day3.assigned.values())
                 if {s for s, o in day2.assigned.items() if o == t} !=
                    {s for s, o in day3.assigned.items() if o == t}}
        assert clash, "v2 is expected to reuse an offset here"


class TestCounterTokenV2Manifest:
    """The manifest is what tomorrow reads, so it is written and found exactly."""

    @staticmethod
    @contextlib.contextmanager
    def _tree(tmp_path):
        """Point data_root() at a throwaway tree for one block."""
        old = os.environ.get("PREMARKET_DATA_ROOT")
        os.environ["PREMARKET_DATA_ROOT"] = str(tmp_path)
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("PREMARKET_DATA_ROOT", None)
            else:
                os.environ["PREMARKET_DATA_ROOT"] = old

    def test_a_written_manifest_reads_back_unchanged(self, tmp_path):
        with self._tree(tmp_path):
            tokens = counter_token.carry_forward(None, ["A", "B", "C"], 10, counter_token.Sequence())
            tokens.free = [7, 9]
            counter_token.write_venue_manifest("20260824", "XCBO", tokens)
            back, stamp = counter_token.previous_tokens("20260825", "XCBO", 10)
            assert stamp == "20260824"
            assert back.assigned == tokens.assigned
            assert back.free == [7, 9]
            assert back.venue_id == 10

    def test_each_venue_owns_its_own_file(self, tmp_path):
        """Databento and Fyers normalize the same day as separate steps.

        With one shared file each step rewrote every venue's allocation; now
        neither can touch the other's, so they cannot clobber each other.
        """
        with self._tree(tmp_path):
            counter_token.write_venue_manifest(
                "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
            counter_token.write_venue_manifest(
                "20260824", "XNSE", counter_token.carry_forward(None, ["N"], 16, counter_token.Sequence()))
            assert counter_token.venues_with_manifest("20260824") == {"XCBO", "XNSE"}
            assert counter_token.venue_entry("20260824", "XNSE")["venue_id"] == 16
            assert set(counter_token.venue_entry("20260824", "XCBO")["assigned"]) == {"A"}

    def test_a_missing_manifest_is_absent_not_an_error(self, tmp_path):
        with self._tree(tmp_path):
            assert counter_token.venue_entry("20260824", "XCBO") == {}
            assert counter_token.previous_tokens("20260825", "XCBO", 10) == (None, "")

    def test_a_corrupt_manifest_is_treated_as_absent(self, tmp_path):
        """A half-written file read as truth would re-issue live tokens."""
        with self._tree(tmp_path):
            path = counter_token.manifests_dir("20260824") / "XCBO.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"version":3,"allocation":{"assig')
            assert counter_token.venue_entry("20260824", "XCBO") == {}

    def test_the_lookback_reaches_across_a_weekend(self, tmp_path):
        with self._tree(tmp_path):
            counter_token.write_venue_manifest(
                "20260821", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
            _, stamp = counter_token.previous_tokens("20260824", "XCBO", 10)
            assert stamp == "20260821"

    def test_the_lookback_stops_rather_than_chaining_a_stale_month(self, tmp_path):
        """Past the window the venue is renumbered, and that shows in the log."""
        with self._tree(tmp_path):
            counter_token.write_venue_manifest(
                "20260101", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
            assert counter_token.previous_tokens("20260824", "XCBO", 10) == (None, "")

    def test_it_takes_the_nearest_day_not_the_first_it_finds(self, tmp_path):
        with self._tree(tmp_path):
            for day, scripts in (("20260820", ["A"]), ("20260821", ["A", "B"])):
                counter_token.write_venue_manifest(
                    day, "XCBO", counter_token.carry_forward(None, scripts, 10, counter_token.Sequence()))
            back, stamp = counter_token.previous_tokens("20260824", "XCBO", 10)
            assert stamp == "20260821" and len(back.assigned) == 2

    def test_a_changed_venue_id_refuses_to_carry_forward(self, tmp_path):
        """venue_id moves the venue onto different blocks, so continuity is gone."""
        with self._tree(tmp_path):
            counter_token.write_venue_manifest(
                "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
            with pytest.raises(ValueError, match="venue_id is 30 in config.ini"):
                counter_token.previous_tokens("20260825", "XCBO", 30)

    def test_the_manifest_is_never_left_half_written(self, tmp_path):
        with self._tree(tmp_path):
            counter_token.write_venue_manifest(
                "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
            leftovers = list(counter_token.manifests_dir("20260824").glob("*.tmp*"))
            assert leftovers == []


class TestCounterTokenV2Validator:
    """The data validator itself (normalize/counter_token_qa.py).

    Built on a two-day tree so each check is exercised against a file that
    really fails it -- a validator nobody has seen fail is a validator that
    passes everything.
    """

    @staticmethod
    def _write(day, mic, rows):
        """One venue's normalized parquet, under whatever the `tree` fixture set."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        directory = paths.normalized_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({
                "script": [r[0] for r in rows],
                # positional, and deliberately allowed to overlap v2
                "counterToken": [str(n) for n in range(1, len(rows) + 1)],
                "counterTokenV2": [r[1] for r in rows],
            }),
            directory / f"{mic}-DATABENTO-normalized.parquet")

    @staticmethod
    def _manifest(day, mic, assigned, venue_id, free=()):
        counter_token.write_venue_manifest(day, mic, counter_token.VenueTokens(
            venue_id=venue_id, assigned=dict(assigned), free=list(free)))

    @staticmethod
    def _named(checks, name):
        return next(c for c in checks if c.name == name)

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    def _good_day(self, day="20260824", scripts=("A", "B", "C")):
        assigned = {s: n for n, s in enumerate(sorted(scripts), 1)}
        rows = [(s, counter_token.assign(n)) for s, n in sorted(assigned.items())]
        self._write(day, "XCBO", rows)
        self._manifest(day, "XCBO", assigned, 10)
        return assigned

    def test_a_clean_day_passes_every_check(self, tree):
        self._good_day()
        checks = counter_token_qa.check_day("20260824", ["XCBO"])
        assert [c.name for c in checks if not c.ok] == []
        assert {"populated", "numeric int32", "one-to-one",
                "manifest internal", "manifest agrees"} <= {c.name for c in checks}

    def test_a_blank_token_fails_populated(self, tree):
        self._write("20260824", "XCBO", [("A", "1"), ("B", "")])
        self._manifest("20260824", "XCBO", {"A": 1, "B": 2}, 10)
        assert not self._named(counter_token_qa.check_day("20260824", ["XCBO"]), "populated").ok

    def test_a_token_past_int32_fails(self, tree):
        self._write("20260824", "XCBO", [("A", str(counter_token.INT32_MAX + 1))])
        self._manifest("20260824", "XCBO", {"A": 1}, 10)
        assert not self._named(counter_token_qa.check_day("20260824", ["XCBO"]), "numeric int32").ok

    def test_two_scripts_sharing_a_token_fails_one_to_one(self, tree):
        """The collision the pg key (token, trade_date) cannot survive."""
        self._write("20260824", "XCBO", [("A", "1"), ("B", "1")])
        self._manifest("20260824", "XCBO", {"A": 1, "B": 1}, 10)
        assert not self._named(counter_token_qa.check_day("20260824", ["XCBO"]), "one-to-one").ok

    def test_a_token_the_manifest_does_not_explain_fails(self, tree):
        self._write("20260824", "XCBO", [("A", "1"), ("B", "2")])
        self._manifest("20260824", "XCBO", {"A": 1}, 10)
        assert not self._named(counter_token_qa.check_day("20260824", ["XCBO"]), "manifest agrees").ok

    def test_a_manifest_token_that_does_not_re_derive_fails(self, tree):
        self._write("20260824", "XCBO", [("A", "99")])
        self._manifest("20260824", "XCBO", {"A": 1}, 10)
        assert not self._named(counter_token_qa.check_day("20260824", ["XCBO"]), "manifest agrees").ok

    def test_an_offset_in_both_free_and_assigned_fails(self, tree):
        self._good_day()
        self._manifest("20260824", "XCBO", {"A": 1, "B": 2, "C": 3}, 10, free=[2])
        assert not self._named(
            counter_token_qa.check_day("20260824", ["XCBO"]), "manifest internal").ok

    def test_a_token_past_int32_in_the_manifest_fails(self, tree):
        """The int32 column makes our own writer unable to emit one of these,
        so the only way in is a table written by something else -- which is
        exactly the case worth checking once the tables are handed over. Built
        by hand with an int64 column, and the header re-hashed to match, so the
        integrity check does not fire first and mask the one under test."""
        import json
        import pyarrow as pa
        import pyarrow.parquet as pq

        self._good_day()
        alloc = counter_token.manifests_dir("20260824") / "XCBO.alloc.parquet"
        pq.write_table(
            pa.table({
                "script": pa.array(["A", "B", "C"], pa.string()),
                "token": pa.array([1, 2, counter_token.INT32_MAX + 1], pa.int64()),
                "state": pa.array(["assigned"] * 3, pa.string()),
            }),
            alloc)
        header = counter_token.manifests_dir("20260824") / "XCBO.json"
        doc = json.loads(header.read_text())
        doc["allocation"]["sha256"] = counter_token.sha256_of(alloc)
        header.write_text(json.dumps(doc))

        assert not self._named(
            counter_token_qa.check_day("20260824", ["XCBO"]), "manifest internal").ok

    def test_a_manifest_with_no_parquet_is_reported(self, tree):
        """The hazard that cost a day of the v3 gap test: deleting a day's data
        does not delete it from the chain, so the next run still carries it."""
        self._manifest("20260824", "XCBO", {"A": 1}, 10)
        paths.normalized_dir("20260824").mkdir(parents=True, exist_ok=True)
        assert not self._named(
            counter_token_qa.check_day("20260824", ["XCBO"]), "manifest has data").ok

    def test_a_parquet_with_no_manifest_is_reported(self, tree):
        self._write("20260824", "XCBO", [("A", "1")])
        assert not self._named(
            counter_token_qa.check_day("20260824", ["XCBO"]), "manifest present").ok

    def test_two_venues_sharing_a_token_fails(self, tree):
        self._good_day()
        self._write("20260824", "XCME", [("Z", "1")])
        self._manifest("20260824", "XCME", {"Z": 1}, 12)
        assert not self._named(counter_token_qa.check_day("20260824"), "venues disjoint").ok

    def test_a_stable_pair_passes(self, tree):
        self._good_day("20260824")
        self._good_day("20260825")
        checks = counter_token_qa.check_pair("20260824", "20260825", ["XCBO"])
        assert [c.name for c in checks if not c.ok] == []

    def test_a_script_that_moved_fails_hard(self, tree):
        """A broken chain, not a v2 quirk -- v2 promises this cannot happen."""
        self._good_day("20260824")
        self._write("20260825", "XCBO", [("A", "2"), ("B", "1")])
        self._manifest("20260825", "XCBO", {"A": 2, "B": 1}, 10)
        moved = self._named(counter_token_qa.check_pair("20260824", "20260825", ["XCBO"]), "stable")
        assert not moved.ok and moved.hard

    def test_a_recycled_token_warns_rather_than_fails(self, tree):
        """v2 does this by design; the count is worth watching, not failing."""
        self._good_day("20260824", ("A", "B", "C"))
        self._write("20260825", "XCBO",
                    [("A", "1"), ("C", "3"), ("D", "2")])   # D took B's number
        self._manifest("20260825", "XCBO", {"A": 1, "C": 3, "D": 2}, 10)
        checks = counter_token_qa.check_pair("20260824", "20260825", ["XCBO"])
        assert self._named(checks, "stable").ok
        reuse = self._named(checks, "no reuse")
        assert not reuse.ok and not reuse.hard and reuse.status == "WARN"

    def test_a_hard_failure_sets_the_exit_code_and_a_warning_does_not(self, tree):
        soft = counter_token_qa.Check("d", "XCBO", "no reuse", False, "", hard=False)
        hard = counter_token_qa.Check("d", "XCBO", "stable", False, "")
        assert counter_token_qa.report([soft]) == 0
        assert counter_token_qa.report([soft, hard]) == 1



class TestLineage:
    """Data lineage (premarketv6/qa/lineage.py): each stage traced to the one before.

    The raw scan needs a real DBN file, so the checks are driven with the scan
    dict _scan_raw produces rather than with databento -- what is under test is
    the reconciliation, not the reader.
    """

    DAY = "20260826"

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _ns(stamp):
        return int(datetime.strptime(stamp, "%Y%m%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1e9)

    @classmethod
    def _file(cls, name, start, end=None, dataset="GLBX.MDP3", schema="definition", records=3):
        return {"name": name, "dataset": dataset, "schema": schema,
                "start": cls._ns(start),
                "end": cls._ns(end) if end else cls._ns(start) + 86_400_000_000_000,
                "records": records}

    @classmethod
    def _scan(cls, files, symbols, duplicates=0, blank=0):
        return {"files": files, "symbols": symbols, "duplicates": duplicates,
                "blank_symbol": blank,
                "records": len(symbols) + duplicates}

    @staticmethod
    def _named(checks, name):
        return next(c for c in checks if c.name == name)

    @staticmethod
    def _parquet(path, columns):
        import pyarrow as pa
        import pyarrow.parquet as pq
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(columns), path)
        return path

    # --- the raw drop ------------------------------------------------------

    def test_a_file_from_the_right_day_passes(self):
        scan = self._scan([self._file("glbx-mdp3-20260826.dbn.zst", "20260826")], {1: "ESZ6"})
        checks = lineage.check_raw(self.DAY, "XCME", [1], scan, "GLBX.MDP3")
        assert [c.name for c in checks if not c.ok] == []

    def test_a_file_from_another_day_fails(self):
        """Found live: 20260826/XNAS held equs-mini-20260825.

        The normalizer checks a file's dataset and schema but not its date, so
        this normalizes cleanly and produces Wednesday output holding Tuesday's
        instruments.
        """
        scan = self._scan([self._file("glbx-mdp3-20260825.dbn.zst", "20260825")], {1: "ESZ6"})
        bad = self._named(lineage.check_raw(self.DAY, "XCME", [1], scan, "GLBX.MDP3"),
                          "raw is this day")
        assert not bad.ok and bad.hard and "2026-08-25" in bad.detail

    def test_a_file_from_another_venue_fails(self):
        """The reader skips it, so its rows are missing rather than wrong."""
        scan = self._scan([self._file("opra.dbn.zst", "20260826", dataset="OPRA.PILLAR")], {})
        assert not self._named(
            lineage.check_raw(self.DAY, "XCME", [1], scan, "GLBX.MDP3"), "raw is this venue").ok

    def test_a_non_definition_schema_fails(self):
        scan = self._scan([self._file("t.dbn.zst", "20260826", schema="trades")], {})
        assert not self._named(
            lineage.check_raw(self.DAY, "XCME", [1], scan, "GLBX.MDP3"), "raw is this venue").ok

    def test_two_files_for_one_day_warn(self):
        """Legal -- the reader stacks them -- but the day is then a blend."""
        scan = self._scan([self._file("a.dbn.zst", "20260826"),
                           self._file("b.dbn.zst", "20260826")], {1: "A"})
        merged = self._named(lineage.check_raw(self.DAY, "XCME", [1, 2], scan, "GLBX.MDP3"),
                             "one session")
        assert not merged.ok and not merged.hard

    def test_a_clamped_window_warns(self):
        """The download stopped at what had published; the file does not say so."""
        scan = self._scan([self._file("a.dbn.zst", "20260826", end="20260826")], {1: "A"})
        clamped = self._named(lineage.check_raw(self.DAY, "XCME", [1], scan, "GLBX.MDP3"),
                              "full day")
        assert not clamped.ok and not clamped.hard

    # --- raw -> normalized -------------------------------------------------

    def _normalized(self, tree, rows, v2=True):
        columns = {
            "script": [r[1] for r in rows],
            "scriptToken": [str(r[0]) for r in rows],
            "def_expiration": [r[2] if len(r) > 2 else "0" for r in rows],
            "expiration": ["0"] * len(rows),
        }
        if v2:
            columns["counterTokenV2"] = [f"11{n}" for n in range(len(rows))]
        return self._parquet(
            paths.normalized_dir(self.DAY) / "XCME-DATABENTO-normalized.parquet", columns)

    def test_the_counts_reconcile(self, tree):
        rows = [(1, "A"), (2, "B")]
        path = self._normalized(tree, rows)
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "A", 2: "B"}, duplicates=5)
        assert self._named(
            lineage.check_normalized(self.DAY, "XCME", path, scan), "row accounting").ok

    def test_a_row_count_that_does_not_reconcile_fails(self, tree):
        path = self._normalized(tree, [(1, "A")])
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "A", 2: "B"})
        assert not self._named(
            lineage.check_normalized(self.DAY, "XCME", path, scan), "row accounting").ok

    def test_blank_symbols_are_counted_as_a_reason_not_a_gap(self, tree):
        """The drop is deliberate, so it has to appear in the arithmetic."""
        path = self._normalized(tree, [(1, "A")])
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "A", 2: ""}, blank=1)
        check = self._named(lineage.check_normalized(self.DAY, "XCME", path, scan),
                            "row accounting")
        assert check.ok and "1 with no symbol" in check.detail

    def test_a_row_with_no_raw_ancestor_fails(self, tree):
        """A normalized row this pipeline invented."""
        path = self._normalized(tree, [(1, "A"), (999, "GHOST")])
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "A", 999: "GHOST"})
        scan["symbols"] = {1: "A"}
        scan["records"] = 1
        assert not self._named(
            lineage.check_normalized(self.DAY, "XCME", path, scan), "no invented rows").ok

    def test_a_symbol_the_mapper_changed_fails(self, tree):
        """The token was allocated against the script; a renamed script moves it."""
        path = self._normalized(tree, [(1, "RENAMED")])
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "ORIGINAL"})
        assert not self._named(
            lineage.check_normalized(self.DAY, "XCME", path, scan), "symbol carried").ok

    def test_a_file_missing_a_column_warns_instead_of_crashing(self, tree):
        """A file written before a column existed still has to be readable.

        Real case: 20260826's XCME output predated a newer column, and the tool
        died on it rather than reporting it. A positional reader of
        NORMALIZED_COLUMNS would read such a file shifted by one.
        """
        path = self._normalized(tree, [(1, "A")], v2=False)
        scan = self._scan([self._file("a.dbn.zst", "20260826")], {1: "A"})
        drift = self._named(lineage.check_normalized(self.DAY, "XCME", path, scan),
                            "schema current")
        assert not drift.ok and not drift.hard and "counterTokenV2" in drift.detail

    # --- normalized -> plugin ----------------------------------------------

    def _plugin(self, names, tokens, extra=None):
        columns = {"token": tokens, "name": names, "trade_date": ["2026-08-26"] * len(names)}
        columns.update(extra or {})
        return self._parquet(
            paths.plugin_dir(self.DAY) / "XCME-DATABENTO-normalized.parquet", columns)

    def test_the_strip_accounts_for_the_difference(self, tree):
        """Expiry is the only reason a normalized row may be absent downstream."""
        expired = str(self._ns("20260101"))
        src = self._normalized(tree, [(1, "A", "0"), (2, "B", expired)])
        out = self._plugin(["A"], ["110"])
        assert self._named(
            lineage.check_plugin(self.DAY, "XCME", src, out), "strip accounting").ok

    def test_a_row_lost_for_no_reason_fails(self, tree):
        src = self._normalized(tree, [(1, "A", "0"), (2, "B", "0")])
        out = self._plugin(["A"], ["110"])
        assert not self._named(
            lineage.check_plugin(self.DAY, "XCME", src, out), "strip accounting").ok

    def test_a_contract_expiring_on_the_trade_date_is_kept(self, tree):
        """0DTE options are live during the session the snapshot describes."""
        src = self._normalized(tree, [(1, "A", str(self._ns("20260826")))])
        out = self._plugin(["A"], ["110"])
        assert self._named(
            lineage.check_plugin(self.DAY, "XCME", src, out), "strip accounting").ok

    def test_a_token_that_changed_between_stages_fails(self, tree):
        """The plugin token must be the counterTokenV2 of the same script."""
        src = self._normalized(tree, [(1, "A", "0")])
        out = self._plugin(["A"], ["999"])
        assert not self._named(
            lineage.check_plugin(self.DAY, "XCME", src, out), "token carried").ok

    def test_an_empty_plugin_value_fails(self, tree):
        """Every plugin column is filled on purpose; an empty one lands as NULL."""
        src = self._normalized(tree, [(1, "A", "0")])
        out = self._plugin(["A"], ["110"], extra={"ticksize": [""]})
        bad = self._named(lineage.check_plugin(self.DAY, "XCME", src, out), "no empty column")
        assert not bad.ok and "ticksize" in bad.detail

    # --- the pg key --------------------------------------------------------

    def test_a_token_shared_by_two_venue_files_fails(self, tree):
        """(token, trade_date) has no venue column to separate them."""
        self._plugin(["A"], ["110"])
        self._parquet(paths.plugin_dir(self.DAY) / "XNAS-DATABENTO-normalized.parquet",
                      {"token": ["110"], "name": ["Z"], "trade_date": ["2026-08-26"]})
        assert not self._named(
            lineage._check_plugin_key(self.DAY, paths.plugin_dir(self.DAY), set()),
            "pg key unique").ok

    def test_distinct_tokens_across_venues_pass(self, tree):
        self._plugin(["A"], ["110"])
        self._parquet(paths.plugin_dir(self.DAY) / "XNAS-DATABENTO-normalized.parquet",
                      {"token": ["150"], "name": ["Z"], "trade_date": ["2026-08-26"]})
        assert self._named(
            lineage._check_plugin_key(self.DAY, paths.plugin_dir(self.DAY), set()),
            "pg key unique").ok



class TestQatReports:
    """Generated QAT reports (premarketv6/qa/report.py).

    Every verdict is tagged with the token column it speaks for, and each tag
    gets its own .txt under paths.qat_dir(). The point is quotable evidence: a
    claim about counterTokenV2 can be cited from v2.txt without the reader
    having to filter a combined log by eye.
    """

    @pytest.fixture
    def out(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_QAT_DIR", str(tmp_path / "QAT_GENERATED"))
        return tmp_path / "QAT_GENERATED"

    @staticmethod
    def _checks():
        return [
            qa_report.Check("20260824", "XCBO", "populated", True, "2,006,525 rows",
                            tag=qa_report.V2),
            qa_report.Check("20260824", "XCBO", "row accounting", True, "reconciles"),
        ]

    def test_one_file_per_tag(self, out):
        qa_report.write_reports(self._checks(), "check-tokens")
        assert sorted(p.name for p in out.iterdir()) == [
            "check-tokens.ALL.txt", "check-tokens.v2.txt"]

    def test_a_tag_file_holds_only_its_own_tag(self, out):
        qa_report.write_reports(self._checks(), "check-tokens")
        body = (out / "check-tokens.v2.txt").read_text()
        assert "populated" in body and "row accounting" not in body

    def test_all_holds_every_line_whatever_its_tag(self, out):
        qa_report.write_reports(self._checks(), "check-tokens")
        body = (out / "check-tokens.ALL.txt").read_text()
        assert all(name in body for name in ("populated", "row accounting"))

    def test_the_header_carries_the_suite_and_its_tally(self, out):
        qa_report.write_reports(self._checks(), "check-tokens")
        head = (out / "check-tokens.v2.txt").read_text().splitlines()[:2]
        assert head[0].startswith("# check-tokens [v2]")
        assert head[1] == "# 1 check(s): 1 pass, 0 fail, 0 warn"

    def test_an_empty_tag_says_so_rather_than_writing_nothing(self, out):
        """A missing file reads as "not run"; an empty one reads as "nothing found"."""
        qa_report.write_reports(
            [qa_report.Check("d", "XCBO", "x", True, "", tag=qa_report.ALL)], "check-lineage")
        assert "no checks carried this tag" in (out / "check-lineage.v2.txt").read_text()

    def test_a_rerun_replaces_rather_than_appends(self, out):
        """The file describes the last run, so a growing file is not evidence."""
        qa_report.write_reports(self._checks(), "check-tokens")
        qa_report.write_reports(self._checks()[:1], "check-tokens")
        assert (out / "check-tokens.ALL.txt").read_text().count("XCBO") == 1

    def test_the_two_suites_do_not_overwrite_each_other(self, out):
        qa_report.write_reports(self._checks(), "check-tokens")
        qa_report.write_reports(self._checks(), "check-lineage")
        assert len(list(out.iterdir())) == 4

    def test_a_line_names_tag_status_day_venue_and_check(self):
        line = qa_report.Check("20260824", "XCBO", "populated", False, "9 blank").line()
        assert line.startswith("[ALL] FAIL")
        assert "20260824" in line and "XCBO" in line and "populated" in line and "9 blank" in line

    def test_a_soft_failure_renders_as_warn(self):
        assert qa_report.Check("d", "V", "n", False, "", hard=False).status == "WARN"

    def test_report_writes_files_only_when_given_a_suite(self, out):
        qa_report.report(self._checks())
        assert not out.exists()
        qa_report.report(self._checks(), suite="check-tokens")
        assert out.exists()


class TestV2Recycling:
    """counterTokenV2's offset recycling, read from the allocation not the tokens.

    The token-level checks cannot see this. A venue whose high_water grows by
    exactly its arrival count every day looks identical to one that recycled
    nothing because it had nothing to recycle -- only the pool arithmetic tells
    them apart, which is the whole reason these exist.
    """

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    SEQ = None

    @classmethod
    def _day(cls, day, scripts, previous=None):
        """Run the real carry_forward and write the manifest it produces."""
        if previous is None:
            cls.SEQ = counter_token.Sequence()
        tokens = counter_token.carry_forward(previous, scripts, 10, cls.SEQ)
        counter_token.write_venue_manifest(day, "XCBO", tokens)
        return tokens

    @staticmethod
    def _named(checks, name):
        return next(c for c in checks if c.name == name)

    def _checks(self):
        return counter_token_qa.check_pair_recycling("20260824", "20260825", "XCBO")

    def test_a_departed_offset_is_released_and_reused(self, tree):
        """B leaves, D arrives and takes B's number without high_water moving."""
        day1 = self._day("20260824", ["A", "B", "C"])
        self._day("20260825", ["A", "C", "D"], day1)
        checks = self._checks()
        assert [c.name for c in checks if not c.ok] == []
        drained = self._named(checks, "pool drained first")
        assert "1 reused a released token" in drained.detail
        assert "0 drew a new one" in drained.detail

    def test_the_pool_is_drained_before_the_sequence_is_drawn_from(self, tree):
        """Two dead tokens, three arrivals: two recycled, one new. Never the reverse."""
        day1 = self._day("20260824", ["A", "B", "C"])
        self._day("20260825", ["A", "D", "E", "F"], day1)
        drained = self._named(self._checks(), "pool drained first")
        assert drained.ok
        assert "2 reused a released token" in drained.detail and "1 drew a new one" in drained.detail

    def test_leftover_offsets_survive_to_the_next_day(self, tree):
        """Three depart, one arrives -- the other two numbers must not be lost."""
        day1 = self._day("20260824", ["A", "B", "C", "D"])
        self._day("20260825", ["A", "Z"], day1)
        carried = self._named(self._checks(), "pool carried")
        assert carried.ok and "2 offset(s) still free" in carried.detail

    def test_a_venue_with_nothing_to_recycle_still_passes(self, tree):
        """XCME's real case: zero departures all week, so the path never engages.

        This must not read as a failure -- there was nothing to reuse.
        """
        day1 = self._day("20260824", ["A", "B"])
        self._day("20260825", ["A", "B", "C"], day1)
        checks = self._checks()
        assert [c.name for c in checks if not c.ok] == []
        assert "0 script(s) departed" in self._named(checks, "offsets released").detail
        assert "1 drew a new one" in self._named(checks, "pool drained first").detail

    def test_drawing_a_new_number_while_the_pool_had_room_fails(self, tree):
        """The failure these checks exist to catch: numbers leaked, not reused."""
        self._day("20260824", ["A", "B", "C"])
        counter_token.write_venue_manifest("20260825", "XCBO", counter_token.VenueTokens(
            venue_id=10,
            assigned={"A": 1, "C": 3, "D": 4},        # D drew a NEW number...
            free=[2]))                                # ...while 2 sat free
        assert not self._named(self._checks(), "pool drained first").ok

    def test_a_lost_leftover_fails(self, tree):
        """Dropping the free pool silently shrinks the venue's usable space."""
        self._day("20260824", ["A", "B", "C", "D"])
        counter_token.write_venue_manifest("20260825", "XCBO", counter_token.VenueTokens(
            venue_id=10, assigned={"A": 1}, free=[]))  # 2, 3, 4 released and lost
        assert not self._named(self._checks(), "pool carried").ok

    def test_a_missing_manifest_warns_rather_than_fails(self, tree):
        self._day("20260824", ["A"])
        absent = self._checks()[0]
        assert not absent.ok and not absent.hard

    def test_every_verdict_is_tagged_v2(self, tree):
        day1 = self._day("20260824", ["A", "B"])
        self._day("20260825", ["A", "C"], day1)
        assert {c.tag for c in self._checks()} == {"v2"}



class TestPerVenueManifest:
    """One manifest per venue. No combined manifest.json, and no fallback to it.

    The fallback was written and then removed on purpose: it makes a day whose
    write failed indistinguishable from a day that legitimately had no
    allocation, and those two want opposite responses.
    """

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _combined(day, venues):
        """A pre-cutover manifest.json, written by hand. Nothing should read it."""
        path = paths.day_dir(day) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 2, "date": day,
            "venues": {mic: {"venue_id": vid, "prefix": vid + 1,
                             "high_water": len(scripts), "count": len(scripts),
                             "free": [], "assigned": {s: n for n, s in enumerate(scripts, 1)}}
                       for mic, (vid, scripts) in venues.items()},
        }))
        return path

    def test_a_venue_writes_to_its_own_file(self, tree):
        counter_token.write_venue_manifest(
            "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
        assert (counter_token.manifests_dir("20260824") / "XCBO.json").exists()
        assert not (paths.day_dir("20260824") / "manifest.json").exists()

    def test_the_combined_manifest_is_not_read(self, tree):
        """The point of "no legacy fallback": an old layout is simply absent."""
        self._combined("20260824", {"XCBO": (10, ["A", "B"])})
        assert counter_token.venue_entry("20260824", "XCBO") == {}
        assert counter_token.venues_with_manifest("20260824") == set()

    def test_an_old_layout_day_renumbers_rather_than_half_chaining(self, tree):
        self._combined("20260824", {"XCBO": (10, ["A", "B"])})
        assert counter_token.previous_tokens("20260825", "XCBO", 10) == (None, "")

    def test_each_venue_owns_its_own_file(self, tree):
        """Databento and Fyers normalize the same day as separate steps.

        With one shared file each step rewrote every venue's allocation; now
        neither can touch the other's, so they cannot clobber each other.
        """
        counter_token.write_venue_manifest(
            "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
        counter_token.write_venue_manifest(
            "20260824", "XNSE", counter_token.carry_forward(None, ["N"], 16, counter_token.Sequence()))
        assert counter_token.venues_with_manifest("20260824") == {"XCBO", "XNSE"}
        assert counter_token.venue_entry("20260824", "XNSE")["venue_id"] == 16
        assert set(counter_token.venue_entry("20260824", "XCBO")["assigned"]) == {"A"}

    def test_one_venues_unreadable_file_does_not_hide_another(self, tree):
        """The combined file made every venue share one blast radius."""
        counter_token.write_venue_manifest(
            "20260824", "XNSE", counter_token.carry_forward(None, ["N"], 16, counter_token.Sequence()))
        (counter_token.manifests_dir("20260824") / "XCBO.json").write_text('{"version":3,"alloc')
        assert counter_token.venue_entry("20260824", "XCBO") == {}
        assert counter_token.venue_entry("20260824", "XNSE")["assigned"] == {"N": 1}

    def test_carry_forward_still_works_across_days(self, tree):
        counter_token.write_venue_manifest(
            "20260824", "XCBO", counter_token.carry_forward(None, ["A", "B"], 10, counter_token.Sequence()))
        previous, stamp = counter_token.previous_tokens("20260825", "XCBO", 10)
        assert stamp == "20260824" and previous.assigned == {"A": 1, "B": 2}

    def test_a_venue_id_change_is_still_refused(self, tree):
        counter_token.write_venue_manifest(
            "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
        with pytest.raises(ValueError, match="venue_id is 30 in config.ini"):
            counter_token.previous_tokens("20260825", "XCBO", 30)

    def test_the_written_file_is_the_new_version(self, tree):
        counter_token.write_venue_manifest(
            "20260824", "XCBO", counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
        doc = json.loads((counter_token.manifests_dir("20260824") / "XCBO.json").read_text())
        assert doc["version"] == counter_token.MANIFEST_VERSION == 4
        assert doc["venue"] == "XCBO" and doc["date"] == "20260824"




class TestSharedSequence:
    """The one counter every venue draws from, and its day-to-day carry.

    This is what replaced the two-digit venue prefix. Nothing in a token says
    which venue issued it any more, so cross-venue uniqueness rests entirely on
    every venue drawing from this.
    """

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    def test_a_day_with_no_history_starts_at_one(self, tree):
        seq, carried = counter_token.open_sequence("20260824")
        assert seq.issued == counter_token.FIRST_TOKEN - 1 and carried == ""
        assert seq.take() == 1

    def test_it_round_trips(self, tree):
        seq = counter_token.Sequence()
        for _ in range(5):
            seq.take()
        counter_token.write_sequence("20260824", seq)
        assert counter_token.load_sequence("20260824") == 5

    def test_a_later_day_continues_yesterdays_numbers(self, tree):
        counter_token.write_sequence("20260824", counter_token.Sequence(2_006_525))
        seq, carried = counter_token.open_sequence("20260825")
        assert carried == "20260824"
        assert seq.take() == 2_006_526

    def test_a_second_run_of_the_same_day_continues_rather_than_restarts(self, tree):
        """The case that matters operationally.

        GLBX definitions publish around 00:00-01:00Z and OPRA around
        10:00-11:00Z, so a day is normalized twice. The second pass must not
        reissue the numbers the first pass already handed out.
        """
        first = counter_token.Sequence()
        counter_token.carry_forward(None, ["A", "B", "C"], 12, first)
        counter_token.write_sequence("20260824", first)

        second, carried = counter_token.open_sequence("20260824")
        assert carried == "20260824"
        later = counter_token.carry_forward(None, ["X", "Y"], 10, second)
        assert set(later.assigned.values()) == {4, 5}

    def test_the_lookback_stops_rather_than_reaching_a_stale_month(self, tree):
        counter_token.write_sequence("20260101", counter_token.Sequence(999))
        seq, carried = counter_token.open_sequence("20260824")
        assert carried == "" and seq.issued == counter_token.FIRST_TOKEN - 1

    def test_the_sequence_file_is_not_mistaken_for_a_venue(self, tree):
        counter_token.write_sequence("20260824", counter_token.Sequence(3))
        counter_token.write_venue_manifest(
            "20260824", "XCBO",
            counter_token.carry_forward(None, ["A"], 10, counter_token.Sequence()))
        assert counter_token.venues_with_manifest("20260824") == {"XCBO"}

    def test_no_two_venues_on_one_day_share_a_number(self, tree):
        seq, _ = counter_token.open_sequence("20260824")
        issued = []
        for venue_id, scripts in ((10, ["A", "B"]), (12, ["A", "B"]), (14, ["A"])):
            issued += list(counter_token.carry_forward(
                None, scripts, venue_id, seq).assigned.values())
        assert len(issued) == len(set(issued)) == 5

    def test_it_is_written_atomically(self, tree):
        counter_token.write_sequence("20260824", counter_token.Sequence(1))
        assert list(counter_token.manifests_dir("20260824").glob("*.tmp*")) == []


class TestVenueCompletionRecord:
    """A venue's manifest IS the answer to "is this venue done for this date".

    The venues do not arrive together, so a day is normalized more than once and
    the early passes legitimately have venues missing. Absent has to mean "not
    done yet", never "empty".
    """

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _write(day, mic, venue_id, started_at=""):
        return counter_token.write_venue_manifest(
            day, mic,
            counter_token.carry_forward(None, ["A"], venue_id, counter_token.Sequence()),
            started_at=started_at)

    def test_a_completed_venue_is_listed(self, tree):
        self._write("20260824", "XCME", 12)
        assert counter_token.venues_with_manifest("20260824") == {"XCME"}

    def test_a_venue_not_yet_run_is_simply_absent(self, tree):
        """XCME lands ~01:00Z and XCBO ~10:30Z; between them XCBO is not done."""
        self._write("20260824", "XCME", 12)
        assert "XCBO" not in counter_token.venues_with_manifest("20260824")
        assert counter_token.venue_run("20260824", "XCBO") == {}

    def test_the_second_pass_adds_without_disturbing_the_first(self, tree):
        self._write("20260824", "XCME", 12)
        self._write("20260824", "XCBO", 10)
        assert counter_token.venues_with_manifest("20260824") == {"XCME", "XCBO"}

    def test_both_timestamps_are_recorded(self, tree):
        self._write("20260824", "XCME", 12, started_at="2026-08-24T01:02:03Z")
        run = counter_token.venue_run("20260824", "XCME")
        assert run["started_at"] == "2026-08-24T01:02:03Z"
        assert run["completed_at"].endswith("Z")

    def test_started_at_defaults_to_completion_when_not_given(self, tree):
        self._write("20260824", "XCME", 12)
        run = counter_token.venue_run("20260824", "XCME")
        assert run["started_at"] == run["completed_at"]

    def test_the_timestamps_are_utc_to_the_second(self, tree):
        stamp = counter_token.utc_now()
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


class TestManifestHeaderAndAllocation:
    """v4: a small JSON header, and the allocation as a Parquet table beside it.

    v3 carried the whole {script: token} map inline, which on an OPRA day was
    64MB of JSON in an 87MB directory. The map is the part that scales with the
    estate; the header is the part a human or a control group actually reads.
    Splitting them is what makes the header readable and what stops every re-run
    of a day from rewriting 64MB -- which matters most under the object
    versioning WORM storage forces on, where each rewrite is another copy that
    cannot be deleted.
    """

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _tokens(assigned, free=(), venue_id=10):
        return counter_token.VenueTokens(
            venue_id=venue_id, assigned=dict(assigned), free=list(free))

    @classmethod
    def _write(cls, day="20260824", mic="XCBO", assigned=None, free=(), **kw):
        return counter_token.write_venue_manifest(
            day, mic, cls._tokens(assigned or {"A": 1, "B": 2}, free), **kw)

    @staticmethod
    def _header(day="20260824", mic="XCBO"):
        return json.loads(
            (counter_token.manifests_dir(day) / f"{mic}.json").read_text())

    @staticmethod
    def _alloc(day="20260824", mic="XCBO"):
        return counter_token.manifests_dir(day) / f"{mic}.alloc.parquet"

    # -- the split itself ------------------------------------------------

    def test_the_allocation_is_not_in_the_header(self, tree):
        self._write()
        assert "assigned" not in self._header()["allocation"]
        assert "free" not in self._header()["allocation"]
        assert self._alloc().exists()

    def test_the_header_stays_small_however_large_the_estate(self, tree):
        """The point of the split. 50,000 scripts inline was ~1.6MB of JSON."""
        self._write(assigned={f"OPRA:SYM{n:06d}": n for n in range(1, 50_001)})
        header = counter_token.manifests_dir("20260824") / "XCBO.json"
        assert header.stat().st_size < 2_000
        assert self._header()["allocation"]["count"] == 50_000

    def test_the_table_round_trips_assigned_and_free(self, tree):
        self._write(assigned={"A": 1, "C": 3}, free=[7, 5])
        entry = counter_token.venue_entry("20260824", "XCBO")
        assert entry["assigned"] == {"A": 1, "C": 3}
        assert sorted(entry["free"]) == [5, 7]
        assert entry["venue_id"] == 10

    def test_venue_entry_keeps_the_shape_its_callers_expect(self, tree):
        """qa/tokens.py and _tokens_from read these five keys and nothing else."""
        self._write(assigned={"A": 1}, free=[9])
        entry = counter_token.venue_entry("20260824", "XCBO")
        assert set(entry) == {"venue_id", "highest", "count", "assigned", "free"}

    def test_a_rerun_of_the_same_day_is_byte_identical(self, tree):
        """A digest over a nondeterministic file would prove nothing."""
        self._write(assigned={"B": 2, "A": 1}, free=[9, 4])
        first = self._alloc().read_bytes()
        self._write(assigned={"A": 1, "B": 2}, free=[4, 9])
        assert self._alloc().read_bytes() == first

    def test_the_table_is_smaller_than_the_json_it_replaces(self, tree):
        assigned = {f"OPRA:SYM{n:06d}": n for n in range(1, 50_001)}
        self._write(assigned=assigned)
        inline = len(json.dumps({"assigned": assigned}))
        assert self._alloc().stat().st_size < inline / 3

    # -- integrity ------------------------------------------------------

    def test_the_header_records_the_tables_digest(self, tree):
        self._write()
        assert (self._header()["allocation"]["sha256"]
                == counter_token.sha256_of(self._alloc()))

    def test_a_table_changed_after_the_fact_is_caught(self, tree):
        import pyarrow as pa
        import pyarrow.parquet as pq

        self._write(assigned={"A": 1, "B": 2})
        pq.write_table(
            pa.table({"script": pa.array(["A", "B"], pa.string()),
                      "token": pa.array([1, 999], pa.int32()),
                      "state": pa.array(["assigned"] * 2, pa.string())}),
            self._alloc())
        with pytest.raises(counter_token.ManifestCorrupt, match="does not match"):
            counter_token.venue_entry("20260824", "XCBO")

    def test_a_header_with_no_table_is_caught(self, tree):
        self._write()
        self._alloc().unlink()
        with pytest.raises(counter_token.ManifestCorrupt, match="no allocation table"):
            counter_token.venue_entry("20260824", "XCBO")

    def test_corruption_is_a_valueerror_so_the_normalizer_skips_the_venue(self, tree):
        """Both normalizers already wrap previous_tokens in `except ValueError`
        and skip. Skipping is the only safe response -- treating a corrupt
        manifest as absent renumbers a live venue from scratch."""
        assert issubclass(counter_token.ManifestCorrupt, ValueError)
        self._write(day="20260824")
        self._alloc("20260824").unlink()
        with pytest.raises(ValueError):
            counter_token.previous_tokens("20260825", "XCBO", 10)

    def test_a_free_token_naming_a_script_is_caught(self, tree):
        self._corrupt_state(tree, ["A"], [1], ["free"], "is free but names script")

    def test_an_assigned_token_naming_no_script_is_caught(self, tree):
        self._corrupt_state(tree, [None], [1], ["assigned"], "names no script")

    def test_an_unknown_state_is_caught(self, tree):
        self._corrupt_state(tree, ["A"], [1], ["retired"], "unknown state")

    def _corrupt_state(self, tree, scripts, tokens, states, match):
        """state is redundant with `script is null` on purpose; this is where
        the redundancy pays, so a table rewritten by something that did not
        understand the convention is caught rather than silently losing a pool."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        self._write()
        pq.write_table(
            pa.table({"script": pa.array(scripts, pa.string()),
                      "token": pa.array(tokens, pa.int32()),
                      "state": pa.array(states, pa.string())}),
            self._alloc())
        header = counter_token.manifests_dir("20260824") / "XCBO.json"
        doc = json.loads(header.read_text())
        doc["allocation"]["sha256"] = counter_token.sha256_of(self._alloc())
        header.write_text(json.dumps(doc))
        with pytest.raises(counter_token.ManifestCorrupt, match=match):
            counter_token.venue_entry("20260824", "XCBO")

    # -- write order and discovery ---------------------------------------

    def test_the_table_is_not_mistaken_for_a_venue(self, tree):
        self._write(mic="XCBO")
        self._write(mic="XCME", assigned={"Z": 9})
        assert counter_token.venues_with_manifest("20260824") == {"XCBO", "XCME"}

    def test_no_staging_file_survives_a_write(self, tree):
        self._write()
        assert list(counter_token.manifests_dir("20260824").glob("*.tmp*")) == []

    def test_a_table_with_no_header_is_not_a_completed_venue(self, tree):
        """The table is written first, so a crash between the two leaves this.
        It must read as not-done, which is what the next run overwrites."""
        self._write()
        (counter_token.manifests_dir("20260824") / "XCBO.json").unlink()
        assert counter_token.venues_with_manifest("20260824") == set()
        assert counter_token.venue_entry("20260824", "XCBO") == {}

    # -- the run record --------------------------------------------------

    def test_the_build_that_wrote_it_is_stamped(self, tree, monkeypatch):
        counter_token.build_sha.cache_clear()
        monkeypatch.setenv("PREMARKET_BUILD_SHA", "deadbeef")
        try:
            self._write()
            assert self._header()["code"]["build_sha"] == "deadbeef"
        finally:
            counter_token.build_sha.cache_clear()

    def test_the_run_record_carries_what_the_numbering_did(self, tree):
        seq = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, seq)
        counter_token.write_venue_manifest(
            "20260824", "XCBO", day1,
            run=counter_token.run_stats(None, day1, seq, "", ""))
        assert self._header()["tokens"]["drawn"] == 3
        assert self._header()["tokens"]["arrived"] == 3

    def test_the_run_record_shows_recycling_rather_than_drawing(self, tree):
        """'0 drawn from the shared sequence' is the whole claim of the design.
        Until now it existed only in a line of stdout."""
        first = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, first)
        second = counter_token.Sequence(first.issued)
        day2 = counter_token.carry_forward(day1, ["A", "D"], 10, second)
        counter_token.write_venue_manifest(
            "20260825", "XCBO", day2,
            run=counter_token.run_stats(day1, day2, second, "20260824", "20260824"))
        record = self._header("20260825")["tokens"]
        assert record["departed"] == 2 and record["released"] == 2
        assert record["arrived"] == 1 and record["reused"] == 1
        assert record["drawn"] == 0
        assert record["sequence_before"] == record["sequence_after"] == 3
        assert record["carried_from"] == "20260824"

    def test_run_stats_counts_a_draw_the_pool_could_not_cover(self, tree):
        first = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A"], 10, first)
        second = counter_token.Sequence(first.issued)
        day2 = counter_token.carry_forward(day1, ["A", "B", "C"], 10, second)
        stats = counter_token.run_stats(day1, day2, second)
        assert stats.arrived == 2 and stats.reused == 0 and stats.drawn == 2

    def test_venue_run_surfaces_the_record(self, tree):
        self._write(started_at="2026-08-24T01:02:03Z")
        run = counter_token.venue_run("20260824", "XCBO")
        assert run["started_at"] == "2026-08-24T01:02:03Z"
        assert "build_sha" in run and "tokens" in run

    # -- the contract the whole thing exists to keep ----------------------

    def test_tokens_still_carry_forward_through_the_new_format(self, tree):
        seq = counter_token.Sequence()
        day1 = counter_token.carry_forward(None, ["A", "B", "C"], 10, seq)
        counter_token.write_venue_manifest("20260824", "XCBO", day1)
        previous, stamp = counter_token.previous_tokens("20260825", "XCBO", 10)
        assert stamp == "20260824"
        assert previous.assigned == day1.assigned
        day2 = counter_token.carry_forward(previous, ["A", "C", "D"], 10, seq)
        assert day2.assigned["A"] == day1.assigned["A"]
        assert day2.assigned["C"] == day1.assigned["C"]
        assert day2.assigned["D"] == day1.assigned["B"]   # recycled


class TestManifestMigration:
    """v3 -> v4 conversion. A format change that must move no token."""

    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _v3(day, mic, assigned, free=(), venue_id=10, **extra):
        directory = counter_token.manifests_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        doc = {"version": 3, "date": day, "venue": mic,
               "started_at": "2026-08-24T01:02:03Z",
               "completed_at": "2026-08-24T01:05:06Z",
               "allocation": {"venue_id": venue_id, "highest": max(assigned.values()),
                              "count": len(assigned), "free": list(free),
                              "assigned": dict(assigned)}}
        doc.update(extra)
        path = directory / f"{mic}.json"
        path.write_text(json.dumps(doc))
        return path

    def test_a_v3_manifest_converts_losslessly(self, tree):
        assigned = {f"S{n}": n for n in range(1, 501)}
        header = self._v3("20260824", "XCBO", assigned, free=[900, 901])
        from premarketv6.normalize import migrate_manifest

        expected = migrate_manifest._v3_tokens(header)
        migrate_manifest.convert(header)
        assert migrate_manifest.verify(header, expected) == []
        entry = counter_token.venue_entry("20260824", "XCBO")
        assert entry["assigned"] == assigned
        assert sorted(entry["free"]) == [900, 901]

    def test_conversion_shrinks_the_directory(self, tree):
        assigned = {f"OPRA:SYM{n:06d}": n for n in range(1, 20_001)}
        header = self._v3("20260824", "XCBO", assigned)
        from premarketv6.normalize import migrate_manifest

        before = header.stat().st_size
        migrate_manifest.convert(header)
        after = header.stat().st_size + (
            counter_token.manifests_dir("20260824") / "XCBO.alloc.parquet").stat().st_size
        assert after < before / 3

    def test_conversion_keeps_the_original_run_times(self, tree):
        """started_at/completed_at say when the venue was NORMALIZED, which this
        does not change. Overwriting them would destroy the only record of it."""
        header = self._v3("20260824", "XCBO", {"A": 1})
        from premarketv6.normalize import migrate_manifest

        migrate_manifest.convert(header)
        run = counter_token.venue_run("20260824", "XCBO")
        assert run["started_at"] == "2026-08-24T01:02:03Z"
        assert run["completed_at"] == "2026-08-24T01:05:06Z"

    def test_conversion_does_not_claim_the_build_that_numbered_the_day(self, tree):
        """v3 recorded no build. Stamping the migration's own sha would assert
        something false about which code produced these tokens."""
        header = self._v3("20260824", "XCBO", {"A": 1})
        from premarketv6.normalize import migrate_manifest

        migrate_manifest.convert(header)
        code = json.loads(header.read_text())["code"]
        assert code["build_sha"] == "unknown"
        assert code["migrated_from"] == 3 and code["migrated_by"]

    def test_verify_catches_a_conversion_that_moved_a_token(self, tree):
        """Mutation check: prove verify() is not vacuous."""
        header = self._v3("20260824", "XCBO", {"A": 1, "B": 2})
        from premarketv6.normalize import migrate_manifest

        migrate_manifest.convert(header)
        wrong = counter_token.VenueTokens(venue_id=10, assigned={"A": 1, "B": 99})
        assert migrate_manifest.verify(header, wrong)

    def test_a_v4_manifest_is_not_reconverted(self, tree):
        counter_token.write_venue_manifest(
            "20260824", "XCBO",
            counter_token.VenueTokens(venue_id=10, assigned={"A": 1}))
        from premarketv6.normalize import migrate_manifest

        header = counter_token.manifests_dir("20260824") / "XCBO.json"
        assert not migrate_manifest._is_v3(header)
        assert migrate_manifest.v3_days(tree) == []

    def test_the_sequence_file_is_not_treated_as_a_venue(self, tree):
        self._v3("20260824", "XCBO", {"A": 1})
        counter_token.write_sequence("20260824", counter_token.Sequence(5))
        from premarketv6.normalize import migrate_manifest

        assert migrate_manifest.run() == 0
        assert counter_token.load_sequence("20260824") == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
