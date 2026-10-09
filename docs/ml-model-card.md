# Model card — ECOReward Bin kiosk detector

Facts below come from the checkpoint's own metadata (`ultralytics` stores it inside `best.pt`) and from
`app.py`. Anything not recorded there is marked **unknown**. Do not change it by hand. Re-generate it from
the checkpoint when the model changes.

## Identity

| | |
|---|---|
| File | `best.pt` (6,268,138 bytes) |
| SHA-256 | `0003698731bd5ebf70d515b882de38008fab0ee4a13cbefc17173dee415dce5a` |
| Architecture | YOLOv8n detector, fine-tuned from COCO-pretrained `yolov8n.pt` |
| Trained | 2026-09-08 04:48 UTC, Ultralytics 8.4.143, on Kaggle |
| Training data | `/kaggle/working/ECOReward-Final-2/data.yaml`. **Not in this repository.** Source, licence, image count and split are **unknown**. |
| Training settings | 100 epochs (early-stop patience 20), imgsz 640, batch 32, optimizer `auto`, lr0 0.01, seed 0, deterministic |

The kiosk records `modelFile` and `modelSha256` on every `detections` document, so each prediction can
be traced to these exact weights.

> `train.py` in this folder did **not** produce `best.pt`. It describes an older 6-class, imgsz-320
> CPU run. The training notebook for this model is not in the repository.

## Classes (20) and waste category

The kiosk lower-cases each label and looks it up in `config/waste_map.json`. If the loaded model has a
class that is not in the map, the kiosk prints `[AI ERROR]` and issues no QR codes.

| id | label | category | | id | label | category |
|---|---|---|---|---|---|---|
| 0 | `1` | Residual | | 10 | `fries` | Biodegradable |
| 1 | `Cigarette` | Residual | | 11 | `metal` | Recyclable |
| 2 | `Fork` | Residual | | 12 | `paper-cup` | Recyclable |
| 3 | `Paper cup` | Recyclable | | 13 | `plain rice` | Biodegradable |
| 4 | `Spoon` | Residual | | 14 | `plastic-cup` | Recyclable |
| 5 | `TISSUE` | Biodegradable | | 15 | `plastic-spoon` | Residual |
| 6 | `bottle` | Recyclable | | 16 | `spoon` | Residual |
| 7 | `bowl` | Biodegradable | | 17 | `straw` | Recyclable |
| 8 | `can` | Recyclable | | 18 | `styro` | Residual |
| 9 | `foam box` | Residual | | 19 | `tissue` | Biodegradable |

### Label-set problems (fix before the next training run)

- `1` (id 0) is not a real object. It is probably an annotation-tool artefact.
- Duplicates that split the training signal: `Paper cup`/`paper-cup`, `Spoon`/`spoon`, `TISSUE`/`tissue`.
- Mapping decisions with no written source yet: `bowl` → Biodegradable, `straw` → Recyclable,
  `metal` vs `can`. Record the local segregation rule each one follows.

## Reported accuracy and what it does *not* show

| Metric (validation split, at end of training) | Value |
|---|---|
| Precision | 0.747 |
| Recall | 0.761 |
| mAP@50 | 0.776 |
| mAP@50-95 | 0.613 |

These figures come from the training run's own validation split. They are **not** measured on the kiosk
camera, its lighting or its background. They score the 20 labels, not the 3 categories that decide
points. No per-class results or confusion matrix were kept.

## How the kiosk turns detections into a decision

1. Inference at `conf=0.15` on the full camera frame.
2. Only boxes with confidence ≥ `MIN_BOX_CONF` (0.30) count. Per frame, only the **single most confident**
   box votes (audit M-20).
3. Majority vote over the last 7 frames. The label is held for 15 frames without a detection.
4. SPACE issues a QR for the voted label's category. It is refused if nothing is detected, if Firebase is
   offline, or if the model does not match the map.

The 0.30 threshold was chosen by hand. It has not been tuned on data.

## Still to do (needs the kiosk and labelled photos)

1. Build a frozen kiosk-camera test set: ≥ 30 photos per category plus empty-bin photos, YOLO-format
   labels, kept apart from all training data.
2. Run `py -3.11 tools/eval_model.py --set <test set> --out docs/eval-best.json` and commit the report.
   The report gives category accuracy, a confusion matrix, the wrong-category QR rate, and a threshold
   chosen by macro-F1. Copy the chosen threshold into `MIN_BOX_CONF` if it differs.
3. Clean the label set (above), retrain once, evaluate on the **same** test set, and record v2 vs v3 here.
4. Commit the training notebook and `data.yaml` (or a link to the exact dataset version).
