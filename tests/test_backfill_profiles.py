# -*- coding: utf-8 -*-
"""Planning logic of tools/backfill_profiles.py (no Firebase)."""
import importlib.util
import unittest
from pathlib import Path

_FILE = Path(__file__).resolve().parents[1] / "tools" / "backfill_profiles.py"
_spec = importlib.util.spec_from_file_location("backfill_under_test", _FILE)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


class BackfillPlanTests(unittest.TestCase):
    def test_legacy_customer_gets_true_lifetime_and_a_profile(self):
        users = {"bob": {"role": "customer", "username": "bob", "points": 70, "badges": ["first_scan"]}}
        redemptions = [{"userId": "bob", "pointsUsed": 50}, {"userId": "alice", "pointsUsed": 99}]
        actions = bf.plan(users, redemptions, profiles={})
        self.assertIn(("set_lifetime", "bob", 120), actions)
        self.assertIn(("set_profile", "bob", {"username": "bob", "points": 70, "lifetimePoints": 120,
                                              "badges": ["first_scan"]}), actions)

    def test_existing_lifetime_is_never_recomputed(self):
        users = {"a": {"role": "customer", "username": "a", "points": 10, "lifetimePoints": 500, "badges": []}}
        actions = bf.plan(users, [{"userId": "a", "pointsUsed": 1}], profiles={})
        self.assertNotIn("set_lifetime", [k for k, _, _ in actions])
        self.assertEqual(actions[0][2]["lifetimePoints"], 500)

    def test_up_to_date_profile_is_left_alone_and_admin_profiles_are_removed(self):
        users = {
            "a": {"role": "customer", "username": "a", "points": 5, "lifetimePoints": 5, "badges": []},
            "adm": {"role": "admin", "username": "boss", "points": 0},
        }
        profiles = {"a": {"username": "a", "points": 5, "lifetimePoints": 5, "badges": []}, "adm": {"username": "boss"}}
        self.assertEqual(bf.plan(users, [], profiles), [("delete_profile", "adm", None)])


if __name__ == "__main__":
    unittest.main()
