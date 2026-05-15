#!/usr/bin/env python3
"""Unit tests for precipitation strategy v1."""

import unittest

from polymarket_precip_dry_run import parse_precip_range
from precip_strategy_v1 import bucket_probability_yes


class ParseTests(unittest.TestCase):
    def test_parse_between_and(self):
        low, high = parse_precip_range("Will NYC have between 2 and 3 inches of precipitation in May?")
        self.assertEqual(low, 2.0)
        self.assertEqual(high, 3.0)

    def test_parse_between_dash(self):
        low, high = parse_precip_range("Will London have between 20-30mm of precipitation in May?")
        self.assertEqual(low, 20.0)
        self.assertEqual(high, 30.0)

    def test_parse_less_than(self):
        low, high = parse_precip_range("Will Seoul have less than 40mm of precipitation in April?")
        self.assertIsNone(low)
        self.assertEqual(high, 40.0)

    def test_parse_or_more(self):
        low, high = parse_precip_range("Will Seattle have 3 inches or more precipitation in May?")
        self.assertEqual(low, 3.0)
        self.assertIsNone(high)


class ProbabilityTests(unittest.TestCase):
    def test_probability_bounds(self):
        p_yes = bucket_probability_yes(10, 20, 5, 25, 10)
        self.assertGreaterEqual(p_yes, 0.0)
        self.assertLessEqual(p_yes, 1.0)

    def test_monotonic_shift(self):
        # Bucket in center should have lower yes probability when forecast shifts far up.
        p_center_low = bucket_probability_yes(10, 20, 5, 25, 5)
        p_center_high = bucket_probability_yes(10, 20, 30, 50, 5)
        self.assertGreater(p_center_low, p_center_high)


if __name__ == "__main__":
    unittest.main()
