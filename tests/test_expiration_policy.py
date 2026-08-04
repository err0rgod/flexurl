import os
import sys
import unittest
from datetime import datetime, timedelta


sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

from expiration_policy import calculate_link_expiration


class TestExpirationPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 16, 12, 0, 0)

    def test_anonymous_link_defaults_to_fifteen_days(self):
        expiration = calculate_link_expiration("free", False, None, self.now)
        self.assertEqual(expiration, self.now + timedelta(days=15))

    def test_anonymous_link_honors_a_shorter_expiration(self):
        expiration = calculate_link_expiration("free", False, 24, self.now)
        self.assertEqual(expiration, self.now + timedelta(days=1))

    def test_anonymous_link_is_capped_at_fifteen_days(self):
        expiration = calculate_link_expiration("free", False, 720, self.now)
        self.assertEqual(expiration, self.now + timedelta(days=15))

    def test_registered_free_link_is_permanent_by_default(self):
        expiration = calculate_link_expiration("free", True, None, self.now)
        self.assertIsNone(expiration)

    def test_registered_free_link_can_use_a_shorter_expiration(self):
        expiration = calculate_link_expiration("free", True, 24, self.now)
        self.assertEqual(expiration, self.now + timedelta(days=1))

    def test_registered_free_link_is_capped_at_fifteen_days(self):
        expiration = calculate_link_expiration("free", True, 720, self.now)
        self.assertEqual(expiration, self.now + timedelta(days=15))

    def test_premium_expiration_behavior_is_unchanged(self):
        self.assertIsNone(calculate_link_expiration("premium", True, None, self.now))
        self.assertEqual(
            calculate_link_expiration("premium", True, 720, self.now),
            self.now + timedelta(days=30),
        )


if __name__ == "__main__":
    unittest.main()
