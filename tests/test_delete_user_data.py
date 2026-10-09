# -*- coding: utf-8 -*-
"""tools/delete_user_data.py: finds every personal-data document of one uid, and only those."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))
_spec = importlib.util.spec_from_file_location("delete_user_data_under_test", _TOOLS / "delete_user_data.py")
du = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(du)


class FakeDb:
    def __init__(self, data):
        self.data = data   # {collection: {doc_id: dict}}

    def collection(self, name):
        db = self

        class Coll:
            def document(self, doc_id):
                exists = doc_id in db.data.get(name, {})
                return types.SimpleNamespace(path=f"{name}/{doc_id}", get=lambda: types.SimpleNamespace(exists=exists))

            def where(self, field, op, value):
                rows = [(i, d) for i, d in db.data.get(name, {}).items() if d.get(field) == value]

                class Q:
                    def stream(self):
                        return [types.SimpleNamespace(reference=types.SimpleNamespace(path=f"{name}/{i}")) for i, _ in rows]
                return Q()
        return Coll()


class PlanTests(unittest.TestCase):
    def test_collects_this_customer_only(self):
        db = FakeDb({
            "users": {"alice": {}, "bob": {}},
            "profiles": {"alice": {}},
            "waste_logs": {"l1": {"userId": "alice"}, "l2": {"userId": "bob"}, "l3": {"userId": "alice"}},
            "redemptions": {"r1": {"userId": "bob"}},
            "qr_scan_history": {"h1": {"scannedByUid": "alice"}},
        })
        found = {c: sorted(r.path for r in refs) for c, refs in du.plan(db, "alice").items()}
        self.assertEqual(found, {
            "users": ["users/alice"], "profiles": ["profiles/alice"],
            "waste_logs": ["waste_logs/l1", "waste_logs/l3"], "redemptions": [],
            "qr_scan_history": ["qr_scan_history/h1"],
        })

    def test_every_collection_with_a_customer_uid_is_covered(self):
        covered = {c for c, _ in du.PERSONAL_DATA}
        self.assertTrue({"users", "profiles", "waste_logs", "redemptions", "qr_scan_history"} <= covered)


if __name__ == "__main__":
    unittest.main()
