"""Unit tests for normalization modules."""
from datetime import date, datetime, timezone

import pytest

from premarketv6 import paths
from premarketv6.normalize import broker_script, databento_norm, fields, price, session
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


class TestFyersNormalization:
    """Test Fyers row mapping to canonical schema."""

    def test_map_fyers_row_equity(self):
        """Test mapping a Fyers equity row."""
        row = {
            "symbol": "INFY",
            "fyToken": "011224010100001",
            "exchange": "NSE",
            "segment": "CM",
            "description": "Infosys Limited",
            "instrumenttype": "EQ",
            "tick_size": "1",
            "lot_size": "1",
        }

        from premarketv6 import config
        cfg = config.load_normalizer()
        result = fields.map_fyers_row(row, cfg)

        assert result["script"] == "INFY"
        assert result["exchange"] == "XNSE"
        assert result["scriptInstrumentType"] == "EQUITY"
        assert result["currency"] == "INR"

    def test_map_fyers_row_future(self):
        """Test mapping a Fyers futures row."""
        row = {
            "symbol": "BANKNIFTY-JAN25FUT",
            "fyToken": "021225010100001",
            "exchange": "NSE",
            "segment": "FO",
            "description": "Bank Nifty Jan 2025",
            "instrumenttype": "FUTSTK",
            "tick_size": "5",
            "lot_size": "15",
            "multiplier": "1",
        }

        from premarketv6 import config
        cfg = config.load_normalizer()
        result = fields.map_fyers_row(row, cfg)

        assert result["script"] == "BANKNIFTY-JAN25FUT"
        assert result["scriptInstrumentType"] == "FUTURE"
        assert result["lotSize"] == 15


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
