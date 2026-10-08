# ECOReward Bin — Python ML Scanner (app.py)

> Part 1 of 3 in the ECOReward Capstone project.
> This is the PC-side smart waste bin scanner.
> It uses a webcam + YOLOv8 to detect waste, generates a QR code,
> writes a token to Firebase, and waits for the Flutter app to scan it.

---

## Project Overview

The ECOReward system has 3 parts that share the same Firebase backend:

| Part | Repo/Folder | Tech |
|------|-------------|------|
| **1. ML Scanner** | `App.py---CASPTONE-master/` (this repo) | Python, YOLOv8, OpenCV, Firebase |
| **2. Mobile App** | `CAPSTONE-1-main/` | Flutter, Dart, Firebase |
| **3. Admin Web** | `CAPSTONE-1-WEB-main/` | Next.js, TypeScript, Firebase |

---

## What This Does

1. Webcam feed runs through YOLOv8 (`best.pt`) to detect waste items
2. Detected item is classified as **Biodegradable**, **Recyclable**, or **Residual**
3. Press **SPACEBAR** → UUID token generated → written to Firestore `qr_tokens` collection
4. QR code displayed on screen (format: `ECOBIN:{wasteType}:{token}`)
5. User scans QR with Flutter app → points awarded → Firestore `usedBy` field updated
6. Firestore listener in `app.py` detects the scan → QR panel auto-dismisses
7. Press **R** during QR display → shows receipt with manual claim code (for users without phone)
8. Users without phone can tell staff their **Gmail + claim code** → staff enters it in Admin Web → points credited

---

## What We Completed

- [x] Full 1280×720 UI with camera HUD, scan frame corners, confidence bar
- [x] QR panel with waste type card, points card, circular arc timer, token card
- [x] Receipt screen with manual claim code (`token[:6]-token[6:10]`)
- [x] Success overlay after QR is scanned
- [x] Firebase integration — reads/writes `qr_tokens` collection
- [x] Firestore real-time listener (`watch_token`) auto-dismisses QR on scan
- [x] QR fix — uses `INTER_NEAREST` resize to keep modules sharp and scannable
- [x] Downloaded `garbage-classification-3` dataset from Roboflow (10,464 images, 6 classes)
- [x] Trained custom YOLOv8n model locally on CPU (5 epochs, imgsz=320)
- [x] `best.pt` trained and working — detects BIODEGRADABLE, CARDBOARD, GLASS, METAL, PAPER, PLASTIC
- [x] Full environment setup on new machine (Python 3.11, PyTorch CPU, all dependencies)
- [x] Flutter mobile app — users can scan QR codes and receive points
- [x] Admin web dashboard — staff can monitor waste logs and scan history

---

## What Still Needs To Be Done

- [x] **Admin Web: Manual Claim page** — staff inputs claim code + user Gmail → system verifies and credits points ✅
- [x] **Connect all 3 parts to same Firebase project** — `.env.local` created with `ecoreward-bin` project config ✅
- [ ] **Test full QR flow end-to-end** — run `app.py`, scan QR with Flutter app, confirm points awarded
- [ ] **Test receipt / manual claim flow** — press R, give Gmail + claim code to staff, verify points credited
- [ ] **Fine-tune model** — 5 epochs is functional; retrain with more epochs for better accuracy on PAPER and PLASTIC classes
- [ ] **Add more waste classes** — PAPER and PLASTIC detection accuracy is low; more training data needed

---

## Setup on a New Machine

### 1. Clone the repo
```bash
git clone https://github.com/JmMoshi16/App.py---CASPTONE.git
cd App.py---CASPTONE
```

### 2. Install Python 3.11
- Download from https://www.python.org/downloads/release/python-3119/
- Run installer — check **"Add Python to PATH"**
- Or use the py launcher: `py install 3.11`

> ⚠️ Python 3.14 is NOT supported — PyTorch does not work on 3.14. Use Python 3.11.

### 3. Install Visual C++ Redistributable
- Required for PyTorch DLLs to load on Windows
- Download from https://aka.ms/vs/17/release/vc_redist.x64.exe
- Run and restart your computer

### 4. Install Python dependencies
```bash
py -3.11 -m pip install ultralytics opencv-python qrcode firebase-admin numpy roboflow
py -3.11 -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

### 5. Add Firebase credentials
- Go to Firebase Console → Project Settings → Service Accounts → Generate new private key
- Save the file as `serviceAccountKey.json.json` (double extension — this is intentional)
- Place it in the same folder as `app.py`

> ⚠️ This file is in `.gitignore` and will NOT be in the repo. You must get it from Firebase manually.

### 6. Get the trained model
**Option A — Get `best.pt` from teammate (recommended)**
- Place `best.pt` in the same folder as `app.py`

**Option B — Retrain locally**
```bash
py -3.11 -m pip install roboflow
py -3.11 download_dataset.py   # pick option 1
py -3.11 train.py
copy runs\detect\ecoreward_cafe-2\weights\best.pt best.pt
```

> ⚠️ `best.pt` and `dataset/` are in `.gitignore` and will NOT be in the repo (too large for GitHub).

### 7. Run the scanner
```bash
py -3.11 app.py
```

---

## Controls

| Key | Action |
|-----|--------|
| `SPACE` | Generate QR for detected item |
| `R` | Toggle receipt screen (while QR is active) |
| `Q` | Quit |

---

## No Phone? Manual Claim Flow

For users who don't have their phone with them:

1. Press `R` during QR display → receipt screen appears with a **manual claim code**
2. User tells staff their **Gmail account** and the **claim code** shown on screen
3. Staff opens the **Admin Web Dashboard** → Manual Claim page
4. Staff enters the claim code + user Gmail → clicks "Claim Points"
5. System verifies the code is valid and not yet used → credits points to that Gmail account

---

## Firebase Structure

### `qr_tokens` collection
```
{
  wasteType: "Recyclable",
  expiresAt: "2025-01-01T12:00:00",
  usedBy: [],           // Flutter app adds user ID here on scan
  createdAt: "2025-01-01T11:40:00"
}
```
Document ID = the UUID token itself.

### QR Code Format
```
ECOBIN:{wasteType}:{token}
```
Example: `ECOBIN:Recyclable:550e8400-e29b-41d4-a716-446655440000`

---

## Model Performance (5 epochs, CPU, imgsz=320)

| Detected Class | Waste Category | Points | mAP50 |
|----------------|---------------|--------|-------|
| BIODEGRADABLE | Biodegradable | 15 pts | 0.507 |
| CARDBOARD | Recyclable | 10 pts | 0.455 |
| GLASS | Recyclable | 10 pts | 0.638 |
| METAL | Recyclable | 10 pts | 0.421 |
| PAPER | Recyclable | 10 pts | 0.066 ⚠️ |
| PLASTIC | Recyclable | 10 pts | 0.079 ⚠️ |
| *(anything else)* | Residual | 5 pts | — |

> ⚠️ PAPER and PLASTIC have low accuracy — more training epochs recommended before final defense.

---

## Files in This Repo

```
App.py---CASPTONE-master/
├── app.py                        # Main scanner application
├── train.py                      # YOLOv8 training script (optional)
├── dataset/
│   └── data.yaml                 # Dataset config (6 classes)
├── serviceAccountKey.json.json   # ⚠️ NOT in repo — get from Firebase
├── best.pt                       # ⚠️ NOT in repo — train locally or get from teammate
└── yolov8n.pt                    # ⚠️ NOT in repo — auto-downloaded by ultralytics
```

---

## Roboflow Dataset

- **Dataset**: Garbage Classification 3
- **Workspace**: `material-identification`
- **Project**: `garbage-classification-3`
- **Version**: 2
- **License**: CC BY 4.0
- **URL**: https://universe.roboflow.com/material-identification/garbage-classification-3/dataset/2

---

## GitHub

- **Repo**: https://github.com/JmMoshi16/App.py---CASPTONE.git
- **Branch**: master
- **Account**: JmMoshi16 / josemanueljurado318@gmail.com
