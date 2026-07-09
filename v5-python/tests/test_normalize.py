"""Unit tests for normalization modules."""
import pytest

from premarket.normalize import databento_norm, fields, price, session
from premarket.sources import fyers_src


class TestPriceScaling:
    """Test price scaling functions."""

    def test_scale_price_default(self):
        """Test scaling with default India scale."""
        # 1.5 * 100000 = 150000
        assert price.scale_price(1.5) == 150000

    def test_scale_price_zero(self):
        """Test scaling zero."""
        assert price.scale_price(0) == 0

    def test_scale_price_negative(self):
        """Test scaling negative prices."""
        assert price.scale_price(-1.0) == -100000

    def test_scale_price_string(self):
        """Test scaling from string input."""
        assert price.scale_price("2.5") == 250000

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
        # Format: EE SS YYMMDD EXTOKEN
        # NSE (01) CM (01) 240101 12345
        token = "010124010112345"
        result = fyers_src.parse_fy_token(token)
        assert result.get("exchange") == "NSE"
        assert result.get("segment") == "CM"

    def test_parse_fy_token_invalid(self):
        """Test parsing invalid fyToken."""
        result = fyers_src.parse_fy_token("invalid")
        assert result == {}

    def test_resolve_exchange_mic(self):
        """Test exchange/segment to MIC mapping."""
        assert fyers_src.resolve_exchange_mic("NSE", "CM") == "XNSE"
        assert fyers_src.resolve_exchange_mic("NSE", "FO") == "XNFO"
        assert fyers_src.resolve_exchange_mic("BSE", "CM") == "XBSE"

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

        from premarket import config
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

        from premarket import config
        cfg = config.load_normalizer()
        result = fields.map_fyers_row(row, cfg)

        assert result["script"] == "BANKNIFTY-JAN25FUT"
        assert result["scriptInstrumentType"] == "FUTURE"
        assert result["lotSize"] == 15


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
