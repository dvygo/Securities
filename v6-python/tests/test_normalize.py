"""Unit tests for normalization modules."""
from datetime import date, datetime, timezone

import pytest

from premarketv6 import paths
from premarketv6.normalize import broker_script, databento_norm, fields, plugin, price, session
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
        symbol-master table postgres_export_plugin appends to -- can collide.
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
