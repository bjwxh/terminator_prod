"""
Unit tests for LiveTradingMonitor._classify_order_type()

Tests cover the five recognized spread structures (single, vertical, butterfly,
condor, iron_condor / iron_fly) and verify that credit/debit structural intent
is returned correctly for each. Also tests unknown/exotic structures.

Run with:
    python -m pytest tests/test_classify_order_type.py -v
"""

import sys
import types
import math
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub only the non-installed packages (schwab and internal server modules).
# pandas, numpy, asyncio, etc. are real and must NOT be stubbed.
# ---------------------------------------------------------------------------

def _make_stub(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# schwab and all sub-packages
for _pkg in [
    "schwab",
    "schwab.auth",
    "schwab.streaming",
    "schwab.utils",
    "schwab.orders",
    "schwab.orders.options",
    "schwab.orders.common",
    "schwab.orders.generic",
]:
    _m = _make_stub(_pkg)
    # give every attribute access a MagicMock so star-imports don't fail
    _m.__all__ = []
    _m.__getattr__ = lambda name, _m=_m: MagicMock()

# Specific names that monitor.py imports explicitly from schwab sub-packages
_schwab_attrs = [
    "easy_client",
    "StreamClient",
    "OrderBuilder",
    "OrderType", "OrderStrategyType", "ComplexOrderStrategyType",
    "Session", "Duration", "OptionInstruction",
    "EquityInstruction",
    "NetCredit", "NetDebit",
]
for _pkg in ["schwab.auth", "schwab.streaming", "schwab.orders.options",
             "schwab.orders.common", "schwab.orders.generic"]:
    for _attr in _schwab_attrs:
        setattr(sys.modules[_pkg], _attr, MagicMock())

# Internal server modules that monitor imports
sys.path.insert(0, "/Users/fw/Git/terminator_prod/server")

# Stub internal modules that don't exist outside the running server
_config_stub = _make_stub("core.config")
_config_stub.CONFIG = {}

_utils_stub = _make_stub("core.utils")
_utils_stub.calculate_delta_decay = MagicMock()

_session_stub = _make_stub("core.session_manager")
_session_stub.SessionManager = MagicMock()

_notif_stub = _make_stub("notifications")
_notif_stub.notify_all = MagicMock()

# ---------------------------------------------------------------------------
# Load monitor.py via importlib so relative imports resolve
# ---------------------------------------------------------------------------

import importlib.util

spec = importlib.util.spec_from_file_location(
    "core.monitor",
    "/Users/fw/Git/terminator_prod/server/core/monitor.py",
)
_monitor_mod = importlib.util.module_from_spec(spec)
sys.modules["core.monitor"] = _monitor_mod

try:
    spec.loader.exec_module(_monitor_mod)
except Exception:
    pass  # tolerate errors in parts of monitor.py we don't need


# Bind the classifier as a plain function (passes None as self — the method
# doesn't use self at all).
_method = _monitor_mod.LiveTradingMonitor._classify_order_type
classify = lambda legs: _method(None, legs)

# ---------------------------------------------------------------------------
# Import the real OptionLeg dataclass
# ---------------------------------------------------------------------------

from core.models import OptionLeg


def leg(side: str, strike: float, qty: int) -> OptionLeg:
    """Minimal OptionLeg factory for tests."""
    return OptionLeg(
        symbol=f"SPXW_{side[0]}{int(strike)}",
        strike=strike,
        side=side,
        quantity=qty,
    )


# ===========================================================================
# Test suites
# ===========================================================================

class TestSingle(unittest.TestCase):

    def test_short_put_is_credit(self):
        order_type, is_credit = classify([leg("PUT", 6000, -1)])
        self.assertEqual(order_type, "single")
        self.assertTrue(is_credit)

    def test_long_put_is_debit(self):
        order_type, is_credit = classify([leg("PUT", 6000, 1)])
        self.assertEqual(order_type, "single")
        self.assertFalse(is_credit)

    def test_short_call_is_credit(self):
        order_type, is_credit = classify([leg("CALL", 6150, -3)])
        self.assertEqual(order_type, "single")
        self.assertTrue(is_credit)

    def test_long_call_is_debit(self):
        order_type, is_credit = classify([leg("CALL", 6150, 2)])
        self.assertEqual(order_type, "single")
        self.assertFalse(is_credit)


class TestVerticalSpread(unittest.TestCase):
    """2-leg spreads: classifier returns None for credit/debit (deferred to mid price)."""

    def test_put_spread_is_vertical(self):
        order_type, is_credit = classify([leg("PUT", 6000, 1), leg("PUT", 6100, -1)])
        self.assertEqual(order_type, "vertical")
        self.assertIsNone(is_credit)

    def test_call_spread_is_vertical(self):
        order_type, is_credit = classify([leg("CALL", 6100, -1), leg("CALL", 6150, 1)])
        self.assertEqual(order_type, "vertical")
        self.assertIsNone(is_credit)

    def test_risk_reversal_mixed_sides_is_vertical(self):
        # +1P6000, -1C6150
        order_type, is_credit = classify([leg("PUT", 6000, 1), leg("CALL", 6150, -1)])
        self.assertEqual(order_type, "vertical")
        self.assertIsNone(is_credit)

    def test_scaled_quantities_are_still_vertical(self):
        order_type, is_credit = classify([leg("PUT", 6000, 5), leg("PUT", 6050, -5)])
        self.assertEqual(order_type, "vertical")
        self.assertIsNone(is_credit)

    def test_two_longs_is_unknown_not_vertical(self):
        order_type, _ = classify([leg("PUT", 6000, 1), leg("PUT", 6100, 1)])
        self.assertEqual(order_type, "unknown")

    def test_ratio_spread_is_unknown_not_vertical(self):
        # +2P6000, -1P6050 — unequal quantities
        order_type, _ = classify([leg("PUT", 6000, 2), leg("PUT", 6050, -1)])
        self.assertEqual(order_type, "unknown")


class TestButterfly(unittest.TestCase):

    def test_long_put_butterfly_is_debit(self):
        # +1P6000, -2P6050, +1P6100
        legs = [leg("PUT", 6000, 1), leg("PUT", 6050, -2), leg("PUT", 6100, 1)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "butterfly")
        self.assertFalse(is_credit)

    def test_short_put_butterfly_is_credit(self):
        # -1P6000, +2P6050, -1P6100
        legs = [leg("PUT", 6000, -1), leg("PUT", 6050, 2), leg("PUT", 6100, -1)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "butterfly")
        self.assertTrue(is_credit)

    def test_long_call_butterfly_is_debit(self):
        # +1C6100, -2C6150, +1C6200
        legs = [leg("CALL", 6100, 1), leg("CALL", 6150, -2), leg("CALL", 6200, 1)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "butterfly")
        self.assertFalse(is_credit)

    def test_short_call_butterfly_is_credit(self):
        # -1C6100, +2C6150, -1C6200
        legs = [leg("CALL", 6100, -1), leg("CALL", 6150, 2), leg("CALL", 6200, -1)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "butterfly")
        self.assertTrue(is_credit)

    def test_scaled_long_butterfly_is_debit(self):
        # +2P6000, -4P6050, +2P6100
        legs = [leg("PUT", 6000, 2), leg("PUT", 6050, -4), leg("PUT", 6100, 2)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "butterfly")
        self.assertFalse(is_credit)

    def test_mixed_side_three_legs_is_unknown(self):
        # +1P6000, -2P6050, +1C6150 — correct ratio but mixed sides → not a butterfly
        legs = [leg("PUT", 6000, 1), leg("PUT", 6050, -2), leg("CALL", 6150, 1)]
        order_type, _ = classify(legs)
        self.assertEqual(order_type, "unknown")

    def test_three_legs_equal_ratio_is_unknown(self):
        # +1P6000, -1P6050, +1P6100 — ratio [1,-1,1], not a butterfly
        legs = [leg("PUT", 6000, 1), leg("PUT", 6050, -1), leg("PUT", 6100, 1)]
        order_type, _ = classify(legs)
        self.assertEqual(order_type, "unknown")


class TestCondor(unittest.TestCase):
    """Same-type (all-put or all-call) condor — 4 legs."""

    def test_long_put_condor_is_debit(self):
        # +1P6000, -1P6050, -1P6100, +1P6150
        legs = [
            leg("PUT", 6000,  1), leg("PUT", 6050, -1),
            leg("PUT", 6100, -1), leg("PUT", 6150,  1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "condor")
        self.assertFalse(is_credit)

    def test_short_put_condor_is_credit(self):
        # -1P6000, +1P6050, +1P6100, -1P6150
        legs = [
            leg("PUT", 6000, -1), leg("PUT", 6050,  1),
            leg("PUT", 6100,  1), leg("PUT", 6150, -1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "condor")
        self.assertTrue(is_credit)

    def test_long_call_condor_is_debit(self):
        # +1C6100, -1C6150, -1C6200, +1C6250
        legs = [
            leg("CALL", 6100,  1), leg("CALL", 6150, -1),
            leg("CALL", 6200, -1), leg("CALL", 6250,  1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "condor")
        self.assertFalse(is_credit)

    def test_scaled_condor_is_debit(self):
        # +3P6000, -3P6050, -3P6100, +3P6150
        legs = [
            leg("PUT", 6000,  3), leg("PUT", 6050, -3),
            leg("PUT", 6100, -3), leg("PUT", 6150,  3),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "condor")
        self.assertFalse(is_credit)


class TestIronCondor(unittest.TestCase):

    def test_standard_ic_is_credit(self):
        # +1P6000, -1P6050, -1C6100, +1C6150
        legs = [
            leg("PUT",  6000,  1), leg("PUT",  6050, -1),
            leg("CALL", 6100, -1), leg("CALL", 6150,  1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "iron_condor")
        self.assertTrue(is_credit)

    def test_reversed_ic_is_debit(self):
        # -1P6000, +1P6050, +1C6100, -1C6150
        legs = [
            leg("PUT",  6000, -1), leg("PUT",  6050,  1),
            leg("CALL", 6100,  1), leg("CALL", 6150, -1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "iron_condor")
        self.assertFalse(is_credit)

    def test_ic_not_misclassified_as_condor(self):
        # Regression: IC has sorted ratio [1,-1,-1,1] same as long condor but
        # must NOT match the same-type condor branch because sides are mixed.
        legs = [
            leg("PUT",  6000,  1), leg("PUT",  6050, -1),
            leg("CALL", 6100, -1), leg("CALL", 6150,  1),
        ]
        order_type, _ = classify(legs)
        self.assertNotEqual(order_type, "condor")

    def test_scaled_ic_is_credit(self):
        # +2P6000, -2P6050, -2C6100, +2C6150
        legs = [
            leg("PUT",  6000,  2), leg("PUT",  6050, -2),
            leg("CALL", 6100, -2), leg("CALL", 6150,  2),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "iron_condor")
        self.assertTrue(is_credit)

    def test_standard_iron_fly_is_credit(self):
        # +1P6000, -1P6050, -1C6050, +1C6100  (shared inner strike → iron fly)
        legs = [
            leg("PUT",  6000,  1), leg("PUT",  6050, -1),
            leg("CALL", 6050, -1), leg("CALL", 6100,  1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "iron_fly")
        self.assertTrue(is_credit)

    def test_long_iron_fly_is_debit(self):
        # -1P6000, +1P6050, +1C6050, -1C6100
        legs = [
            leg("PUT",  6000, -1), leg("PUT",  6050,  1),
            leg("CALL", 6050,  1), leg("CALL", 6100, -1),
        ]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "iron_fly")
        self.assertFalse(is_credit)


class TestUnknown(unittest.TestCase):

    def test_empty_is_unknown(self):
        order_type, is_credit = classify([])
        self.assertEqual(order_type, "unknown")
        self.assertIsNone(is_credit)

    def test_five_legs_is_unknown(self):
        legs = [leg("PUT", 5900 + i * 50, 1 if i % 2 == 0 else -1) for i in range(5)]
        order_type, is_credit = classify(legs)
        self.assertEqual(order_type, "unknown")
        self.assertIsNone(is_credit)

    def test_ratio_spread_is_unknown(self):
        # +2P6000, -1P6050 — unequal quantities
        order_type, _ = classify([leg("PUT", 6000, 2), leg("PUT", 6050, -1)])
        self.assertEqual(order_type, "unknown")

    def test_four_legs_all_same_direction_is_unknown(self):
        # All long — not a valid condor
        legs = [leg("PUT", 6000 + i * 50, 1) for i in range(4)]
        order_type, _ = classify(legs)
        self.assertEqual(order_type, "unknown")

    def test_ic_missing_one_put_is_unknown(self):
        # Only 1 put + 2 calls — not a recognized structure
        legs = [
            leg("PUT",  6000,  1),
            leg("CALL", 6100, -1), leg("CALL", 6150,  1),
        ]
        order_type, _ = classify(legs)
        # 3 legs with ratio [1,-1,1] → not a butterfly ratio either
        self.assertEqual(order_type, "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
