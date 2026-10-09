#!/usr/bin/env python3
"""Evaluate a kiosk model on a frozen, labelled test set (audit H-05).

The test set must be YOLO format, labelled with the SAME class list as the model:
    <set>/images/*.jpg|png
    <set>/labels/<same name>.txt     (empty file = no waste in the picture)

Each image is scored the way the kiosk decides: the single most confident box at or above
the threshold (audit M-20); no box => "none". Ground truth = class of the largest labelled box.
Points depend only on the waste category, so the headline number is CATEGORY accuracy.

Usage:
    py -3.11 tools/eval_model.py --set path/to/kiosk_test_set [--model best.pt] [--out report.json]
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
WASTE_MAP = json.loads((APP_DIR / "config" / "waste_map.json").read_text(encoding="utf-8"))["map"]
NONE = "none"
THRESHOLDS = [round(0.05 * i, 2) for i in range(3, 19)]   # 0.15 .. 0.90


def category(label):
    return NONE if label == NONE else WASTE_MAP.get(label.lower(), "UNMAPPED")


def decide(boxes, threshold):
    """boxes: [(label, conf), ...] -> label of the top box >= threshold, else 'none'."""
    best = max((b for b in boxes if b[1] >= threshold), key=lambda b: b[1], default=None)
    return best[0].lower() if best else NONE


def score(records, threshold):
    """records: [{'truth': label|'none', 'boxes': [(label, conf), ...]}, ...] -> metrics dict."""
    confusion = defaultdict(Counter)            # truth category -> predicted category -> n
    label_hits = cat_hits = 0
    for r in records:
        pred = decide(r["boxes"], threshold)
        truth = r["truth"].lower()
        label_hits += pred == truth
        cat_hits += category(pred) == category(truth)
        confusion[category(truth)][category(pred)] += 1

    cats = sorted({c for row in confusion.values() for c in row} | set(confusion))
    per_cat, f1s = {}, []
    for c in cats:
        tp = confusion[c][c]
        fp = sum(confusion[t][c] for t in cats if t != c)
        fn = sum(confusion[c][p] for p in cats if p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per_cat[c] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4), "support": tp + fn}
        if c != NONE:
            f1s.append(f1)
    # A QR with the wrong category is the costly mistake: it awards the wrong points.
    issued = [(category(r["truth"].lower()), category(decide(r["boxes"], threshold))) for r in records]
    issued = [(t, p) for t, p in issued if p != NONE]
    n = len(records) or 1
    return {
        "threshold": threshold,
        "images": len(records),
        "label_accuracy": round(label_hits / n, 4),
        "category_accuracy": round(cat_hits / n, 4),
        "macro_f1_waste_categories": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "wrong_category_qr_rate": round(sum(t != p for t, p in issued) / len(issued), 4) if issued else 0.0,
        "per_category": per_cat,
        "confusion": {t: dict(row) for t, row in sorted(confusion.items())},
    }


def sweep(records):
    reports = [score(records, t) for t in THRESHOLDS]
    best = max(reports, key=lambda r: (r["macro_f1_waste_categories"], -r["wrong_category_qr_rate"]))
    return {"chosen_threshold": best["threshold"], "chosen": best,
            "sweep": [{k: r[k] for k in ("threshold", "category_accuracy", "macro_f1_waste_categories",
                                          "wrong_category_qr_rate")} for r in reports]}


def load_records(set_dir, model):
    names = {int(k): v for k, v in model.names.items()}
    images = sorted(p for p in (set_dir / "images").iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    records = []
    for img in images:
        lbl_file = set_dir / "labels" / (img.stem + ".txt")
        if not lbl_file.exists():
            raise SystemExit(f"[ERROR] missing label file for {img.name}")
        rows = [ln.split() for ln in lbl_file.read_text().splitlines() if ln.strip()]
        truth = names[int(max(rows, key=lambda r: float(r[3]) * float(r[4]))[0])] if rows else NONE
        res = model(str(img), conf=0.05, verbose=False)[0]
        boxes = [(names[int(b.cls[0])], float(b.conf[0])) for b in res.boxes]
        records.append({"image": img.name, "truth": truth, "boxes": boxes})
    return records


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", required=True, type=Path, help="test-set folder with images/ and labels/")
    ap.add_argument("--model", default=str(APP_DIR / "best.pt"))
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    args = ap.parse_args()

    import hashlib
    from ultralytics import YOLO
    model = YOLO(args.model)
    unmapped = sorted({n.lower() for n in model.names.values()} - set(WASTE_MAP))
    if unmapped:
        raise SystemExit(f"[ERROR] model classes missing from config/waste_map.json: {unmapped}")

    report = {
        "model": Path(args.model).name,
        "model_sha256": hashlib.sha256(Path(args.model).read_bytes()).hexdigest(),
        "test_set": str(args.set),
        **sweep(load_records(args.set, model)),
    }
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"[DONE] report written to {args.out} - chosen threshold {report['chosen_threshold']}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
