# -*- coding: utf-8 -*-
"""Scoring logic of tools/eval_model.py on hand-checked records (no model, no images)."""
import importlib.util
import unittest
from pathlib import Path

_FILE = Path(__file__).resolve().parents[1] / "tools" / "eval_model.py"
_spec = importlib.util.spec_from_file_location("eval_model_under_test", _FILE)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

RECORDS = [
    {"truth": "can",    "boxes": [("can", 0.90)]},                    # right label, right category
    {"truth": "fork",   "boxes": [("spoon", 0.80)]},                  # wrong label, right category (Residual)
    {"truth": "bottle", "boxes": [("fries", 0.60), ("bottle", 0.40)]},  # top box wins -> wrong category
    {"truth": "none",   "boxes": []},                                 # empty scene, nothing issued
    {"truth": "tissue", "boxes": [("tissue", 0.20)]},                 # only found at a low threshold
]


class EvalScoringTests(unittest.TestCase):
    def test_metrics_at_kiosk_threshold(self):
        r = ev.score(RECORDS, 0.30)
        self.assertEqual(r["label_accuracy"], 0.4)
        self.assertEqual(r["category_accuracy"], 0.6)
        self.assertEqual(r["wrong_category_qr_rate"], 0.3333)   # 1 of 3 issued QRs has the wrong category
        self.assertEqual(r["confusion"]["Recyclable"], {"Recyclable": 1, "Biodegradable": 1})
        self.assertEqual(r["confusion"]["Biodegradable"], {"none": 1})

    def test_sweep_picks_the_threshold_with_best_macro_f1(self):
        s = ev.sweep(RECORDS)
        self.assertEqual(s["chosen_threshold"], 0.15)
        self.assertEqual(s["chosen"]["category_accuracy"], 0.8)
        self.assertEqual(len(s["sweep"]), len(ev.THRESHOLDS))

    def test_unknown_labels_are_reported_not_hidden(self):
        self.assertEqual(ev.category("person"), "UNMAPPED")


if __name__ == "__main__":
    unittest.main()
