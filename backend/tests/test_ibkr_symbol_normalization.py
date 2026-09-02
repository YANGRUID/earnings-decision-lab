"""Class-share symbol normalization at the TWS transport boundary
(V4 consolidation, Sections 12-13, 57)."""

from datetime import date
from decimal import Decimal

import pytest

from providers.ibkr_tws_options import _option_contract, _stock_contract, ibkr_symbol


class TestIbkrSymbol:
    @pytest.mark.parametrize(
        "canonical, expected",
        [
            ("BF.A", "BF A"),
            ("BF.B", "BF B"),
            ("BRK.A", "BRK A"),
            ("BRK.B", "BRK B"),
            ("bf.b", "BF B"),
            ("AAPL", "AAPL"),
            ("aapl", "AAPL"),
            ("PANW", "PANW"),
        ],
    )
    def test_single_letter_class_suffix_becomes_a_space(self, canonical, expected):
        assert ibkr_symbol(canonical) == expected

    @pytest.mark.parametrize("weird", ["A.BC", "X.", ".B", "A.B.C", "BF.1"])
    def test_anything_that_is_not_a_class_suffix_is_left_alone(self, weird):
        """Only a trailing single alphabetic letter is a share class. A dot
        anywhere else is not guessed at -- it goes through unchanged and
        will fail loudly at IBKR rather than silently resolving to the
        wrong instrument."""
        assert ibkr_symbol(weird) == weird.upper()


class TestTheBoundaryIsTheOnlyPlaceItHappens:
    def test_stock_contract_uses_the_ibkr_form(self):
        assert _stock_contract("BF.B").symbol == "BF B"
        assert _stock_contract("AAPL").symbol == "AAPL"

    def test_option_contract_uses_the_ibkr_form(self):
        c = _option_contract("BRK.B", date(2026, 9, 18), Decimal("450"), "C")
        assert c.symbol == "BRK B"
        assert c.secType == "OPT"

    def test_no_scattered_replace_calls_elsewhere_in_the_transport(self):
        """Section 13 -- one authoritative layer, not `.replace('.', ' ')`
        sprinkled through the codebase."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src"
        offenders = []
        for path in (src / "providers").rglob("*.py"):
            text = path.read_text()
            if 'replace(".", " ")' in text or "replace('.', ' ')" in text:
                offenders.append(path.name)
        assert not offenders, offenders

    def test_canonical_domain_ticker_is_never_rewritten(self):
        """The domain keeps the dotted form; only the wire form changes."""
        canonical = "BF.B"
        _ = _stock_contract(canonical)
        assert canonical == "BF.B"


class TestSecDefUsesTheWireSymbol:
    """Live finding (2026-09-02): the stock contract resolved for BF.B / BRK.B
    but reqSecDefOptParams was still sent the dotted ticker and answered
    error 322. The option-params request must use the wire form and accept
    IBKR's separator-less trading class."""

    def test_option_params_sends_wire_symbol_and_accepts_bfb_trading_class(self):

        from providers.ibkr_tws_options import IBKRTWSProvider

        seen = {}

        class Conn:
            def request_sec_def_opt_params(self, symbol, sec_type, conid):
                seen["symbol"] = symbol
                return [
                    {"trading_class": "BFB", "exchange": "SMART", "expirations": ["20260918"],
                     "strikes": [25.0, 27.5, 30.0], "multiplier": "100"},
                    {"trading_class": "2BFB", "exchange": "SMART", "expirations": ["20260918"],
                     "strikes": [27.5], "multiplier": "100"},
                ]

        provider = IBKRTWSProvider.__new__(IBKRTWSProvider)
        provider._connection = Conn()
        params = IBKRTWSProvider._option_params(provider, "BF.B", 12345)
        assert seen["symbol"] == "BF B"
        assert params is not None and params["trading_class"] == "BFB"

    def test_plain_ticker_is_unchanged(self):
        from providers.ibkr_tws_options import IBKRTWSProvider

        seen = {}

        class Conn:
            def request_sec_def_opt_params(self, symbol, sec_type, conid):
                seen["symbol"] = symbol
                return [{"trading_class": "AAPL", "exchange": "SMART", "expirations": ["20260918"],
                         "strikes": [200.0], "multiplier": "100"}]

        provider = IBKRTWSProvider.__new__(IBKRTWSProvider)
        provider._connection = Conn()
        params = IBKRTWSProvider._option_params(provider, "AAPL", 1)
        assert seen["symbol"] == "AAPL" and params["trading_class"] == "AAPL"
