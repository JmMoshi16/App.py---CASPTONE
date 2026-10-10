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

1. Webcam feed runs through YOLOv8 (`best.pt`) to detect waste items (confidence ≥ 0.30, 7-frame majority vote)
2. Detected item is classified as **Biodegradable**, **Recyclable**, or **Residual** (`config/waste_map.json`)
3. The deposit is confirmed — by a **staff member pressing SPACE** (attendant mode, the default) or, with the
   ESP32 sorter, by the **servo acknowledging and the load cell registering the drop**
4. Only then a UUID token is written to Firestore `qr_tokens`; the QR (`ECOBIN:{wasteType}:{token}`) is shown
   only after that write succeeded
5. User scans the QR with the Flutter app → the app credits points → the kiosk sees the claim and shows "Claimed!"
6. No phone? Tap **"No phone? Claim code"** (or staff press **R**) → receipt with a manual claim code
7. Staff enter **Gmail + claim code** in the Admin Web → points credited

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

### 5. Connect the kiosk to Firebase — kiosk identity (recommended)
The kiosk signs in as its **own** Firebase user with the claim `kiosk: true`. The Firestore rules let that
identity do only four things: create a well-formed QR token, read a token by id, write its own heartbeat and
add a detection. A stolen kiosk PC therefore **cannot read customer e-mails, points or redemptions**, and you
cut it off by revoking one user. Its config file holds a public Web API key and a revocable sign-in token —
no password and no private key.

**Before the first time:** deploy the Phase 7 rules (`firebase/README.md`). The kiosk identity depends on them.

On an **admin's computer** (the one that has the service-account key — see "Admin key" below):
```bash
py -3.11 tools\set_claims.py create-kiosk --name cafe-kiosk-1 --api-key <Web API key>
```
The Web API key is in Firebase Console → Project settings → General (it is public; it also ships in the app).
This writes `%USERPROFILE%\.ecoreward\kiosk.json` (readable only by you). Then:

1. Copy `kiosk.json` to the **kiosk PC** at `C:\Users\<kiosk user>\.ecoreward\kiosk.json`
   (or anywhere, and point to it with `setx ECOREWARD_KIOSK_CONFIG "C:\path\kiosk.json"`).
2. **Remove the service-account key from the kiosk PC.**
3. Start the scanner. The first lines must say `[FIREBASE] Connected as the kiosk identity (least privilege).`
4. Kiosk lost, stolen or retired: `py -3.11 tools\set_claims.py revoke-kiosk cafe-kiosk-1` (takes effect within an hour).

While a QR is on screen, the kiosk checks every 2 s (every 5 s after 2 minutes) whether it was scanned — the
REST API it uses has no live listener. That is at most a few hundred reads per unscanned QR.

#### Admin key (admin computers and the maintenance tools only)
`tools\set_claims.py`, `tools\backfill_profiles.py`, `tools\delete_user_data.py` and `tools\migrate_timestamps.py`
use a **service-account key**. It is a full-access secret (it bypasses all Firestore rules): keep it on an
admin's computer, **outside this project folder**.

1. Firebase Console → Project Settings → Service Accounts → **Generate new private key**.
2. Save it at `C:\Users\<you>\.ecoreward\kiosk-sa.json` (create the folder), or anywhere you like and point to it
   once with an environment variable, then open a new terminal:
   ```bash
   setx GOOGLE_APPLICATION_CREDENTIALS "C:\path\to\kiosk-sa.json"
   ```

If there is **no** `kiosk.json`, the scanner still falls back to this key (legacy mode) and prints a
`[SECURITY]` warning — that mode can read every user, so switch to the kiosk identity.
Lookup order: `ECOREWARD_KIOSK_CONFIG` / `~/.ecoreward/kiosk.json` (kiosk identity) → `GOOGLE_APPLICATION_CREDENTIALS`
→ `~/.ecoreward/kiosk-sa.json` → *(legacy, deprecated)* `serviceAccountKey.json[.json]` next to `app.py`.

If no credential is found the scanner runs **OFFLINE**: a red banner is shown and SPACE will **not** issue
QR codes, because a QR for a token that was never stored can never be redeemed.

> ⚠️ Never commit, zip, e-mail or chat the key. If it was ever in a shared folder, create a new key and
> **delete the old key ID** (Google Cloud Console → IAM & Admin → Service Accounts → Keys). Deleting the
> file alone does not revoke copies.

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

### 7. Run the kiosk
```bash
py -3.11 app.py                              # attendant mode, 1280x720 window
py -3.11 app.py --fullscreen                 # fill the kiosk display (1024x600 panel, laptop, monitor)
py -3.11 app.py --sorter esp32 --fullscreen  # ESP32 servo + load cell (see "ESP32 protocol")
py -3.11 app.py --no-hardware                # simulated ESP32 for demos — labelled SIMULATION on screen
py -3.11 app.py --help                       # every option
```

| Sorter mode | Who confirms the deposit | Use it when |
|---|---|---|
| `attendant` (default) | a staff member presses **SPACE** | no sorting hardware connected; the original kiosk flow |
| `esp32` | the ESP32: servo `OK`, then the load-cell weight | the firmware speaks the protocol below |
| `sim` | a scripted ESP32 (OK after 0.6 s, 42 g after 1.6 s) | demos and tests; never writes sensor data to Firestore |

Defaults live in `config/kiosk_settings.json` (display size, camera index, sorter mode, serial port, timeouts,
reduced motion, sounds); command-line flags override them. If the internet drops, the ESP32 still sorts and the
kiosk explains that no reward code could be created; in attendant mode SPACE is refused while offline.

### 8. Run the tests (no camera, no Firebase, no model needed)
```bash
py -3.11 -I -m unittest discover -s tests -v
```
They replace Firebase, the YOLO model and the camera window with fakes and exercise the real main loop.

---

## Controls

Customers only use the touch screen (**No phone? Claim code**, **Done**, **OK**, **Back to the QR code**).
Staff keys:

| Key | Action |
|-----|--------|
| `SPACE` | Confirm the deposit (attendant mode). In `esp32`/`sim` mode only with `--debug` (manual sort) |
| `R` | Show / hide the claim-code receipt (while a QR is shown) |
| `H` | Session history (codes claimed since the kiosk started) |
| `D` | Diagnostics: Firestore mode, model + SHA-256, camera FPS, inference ms, sorter port/state/last lines, fill levels |
| `Esc` | Close the overlay |
| `Q` | Quit (closing the window also quits cleanly) |

A QR stays up until it is scanned, **Done** is tapped, it expires, or 3 minutes pass without a scan
(`qr_display_seconds`) — the code itself stays valid for the full 20 minutes.

## ESP32 protocol (sorter mode `esp32`)

> **Assumption — confirm against the firmware.** No sketch is in this repository; this is the protocol the kiosk
> implements (`kiosk/hardware.py`). Lines are ASCII, newline-terminated, 115200 baud by default.

| Direction | Line | Meaning |
|---|---|---|
| kiosk → ESP32 | `1` / `2` / `3` | move the chute to Biodegradable / Recyclable / Residual |
| ESP32 → kiosk | `OK` (or `ACK`) | the servo reached the chute (kiosk waits ≤ 5 s) |
| ESP32 → kiosk | `W:<grams>` e.g. `W:42.5` | HX711 reading after the drop (kiosk waits ≤ 10 s; < 5 g = nothing dropped) |
| ESP32 → kiosk | `F:<bio>,<rec>,<res>` e.g. `F:45,22,68` | HC-SR04 fill levels in % — written to `bins/{kioskId}-bio/rec/res` for the web dashboard |
| ESP32 → kiosk | `ERR:<text>` | a fault; the customer sees "The sorter reported a problem" |

Firmware checklist: print `OK` once the servo is in place; print one `W:` line after the weight settles;
send `F:` every ~10 s; never move the servo without a command. The kiosk never sends a command when it
connects or reconnects, drops commands queued while the ESP32 was unplugged, and finds the port automatically
(CP210x, CH340, FTDI or ESP32-S native USB) unless `serial_port` / `--port COM3` is set. PySerial is required:
`py -3.11 -m pip install pyserial`. The `bins` writes need the rules in `firebase/firestore.rules` to be deployed.

## Faster inference (OpenVINO, optional)

```bash
py -3.11 -m pip install openvino
py -3.11 -c "from ultralytics import YOLO; YOLO('best.pt').export(format='openvino')"
```
This creates `best_openvino_model/`; the kiosk loads it automatically when it exists (diagnostics shows the
file and its SHA-256). Check accuracy with `tools/eval_model.py` before relying on it.

## Logs and diagnostics

Everything printed to the console is also written to `logs/kiosk.log` (one file per day, 14 days kept).
Repeating problems (camera gone, sorter reconnecting, Firestore unreachable) are logged once, not every frame.

---

## No Phone? Manual Claim Flow

For users who don't have their phone with them:

1. Tap **No phone? Claim code** (or press `R`) during QR display → receipt appears with a **manual claim code**
2. User tells staff their **Gmail account** and the **claim code** shown on screen
3. Staff opens the **Admin Web Dashboard** → Manual Claim page
4. Staff enters the claim code + user Gmail → clicks "Claim Points"
5. System verifies the code is valid and not yet used → credits points to that Gmail account

---

## Firebase Structure

### `qr_tokens` collection (written by the kiosk; shape enforced by `isNewKioskToken()` in the rules)
```
{
  wasteType: "Recyclable",          // Biodegradable | Recyclable | Residual
  expiresAt: <Timestamp>,           // createdAt + 20 min (config/rules.json); a Timestamp so the rules check expiry with server time
  createdAt: <server Timestamp>,
  usedBy: [],                       // the Flutter app adds the customer's uid when they claim it
  isUsed: false,
  claimCode: "4821"                 // shown on the receipt as ECO-4821 for manual claims
}
```
Document ID = the UUID token itself. The kiosk also writes `detections/{id}` (one per issued code),
`kiosks/{kioskId}` (heartbeat every 30 s, `offline` on a clean exit) and, with a real ESP32, `bins/{kioskId}-{bio|rec|res}`.
It never writes `users`, `waste_logs`, `redemptions` or `profiles`.

### QR Code Format
```
ECOBIN:{wasteType}:{token}
```
Example: `ECOBIN:Recyclable:550e8400-e29b-41d4-a716-446655440000`

---

## Model

The shipped `best.pt` is a 20-class YOLOv8n model (imgsz 640, 100 epochs, trained on Kaggle) — see
`docs/ml-model-card.md` for its classes, validation results and limits. `train.py` is a legacy 6-class script
that did **not** produce it. Every class must be listed in `config/waste_map.json`, or the kiosk refuses to
issue codes and says so on screen.

---

## Files in This Repo

```
App.py---CASPTONE-master/
├── app.py                        # Entry point: camera, model, Firestore, main loop
├── kiosk/                        # config.py (settings), hardware.py (sorters), flow.py (deposit state machine)
│   └── ui/                       # theme, drawing, icons, components, screens (DESIGN_SYSTEM.md)
├── assets/fonts/                 # DM Sans + Fraunces (SIL OFL, same as the ECOReward app)
├── config/                       # rules.json (shared business rules), waste_map.json, kiosk_settings.json
├── DESIGN_SYSTEM.md              # kiosk visual and interaction specification
├── train.py                      # legacy training script (did not produce best.pt)
├── kiosk_firestore.py            # least-privilege Firestore client used with the kiosk identity
├── (kiosk.json)                  # ⚠️ NOT in repo — kiosk PC only: ~/.ecoreward/kiosk.json (tools/set_claims.py)
├── (kiosk-sa.json)               # ⚠️ NOT in repo — ADMIN PC only, OUTSIDE the project: ~/.ecoreward/kiosk-sa.json
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
