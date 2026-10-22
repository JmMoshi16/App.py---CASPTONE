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

1. A webcam inside the chamber looks down at the tray; YOLOv8 (`best.pt`) recognises the item (confidence ≥ 0.30, 7-frame majority vote)
2. Detected item is classified as **Biodegradable**, **Recyclable**, or **Residual** (`config/waste_map.json`)
3. The customer drops one item onto a tray that sits on a load cell; once the weight is steady, YOLO identifies it
   on the tray crop and the ESP32 sorts it into the right bin and reports `DONE` (see "How a deposit works")
4. Only then a UUID token is written to Firestore `qr_tokens`; the QR (`ECOBIN:{wasteType}:{token}`) is shown
   only after that write succeeded
5. User scans the QR with the Flutter app → the app credits points → the kiosk sees the claim and shows "Claimed!"
6. No phone? Tap **"No phone? Claim code"** (or staff press **R**) → receipt with a manual claim code
7. Staff enter the **claim code + username** in the Admin Web, confirm the account by its email → points credited

---

## What We Completed

- [x] Full 1280×720 UI with camera HUD, scan frame corners, confidence bar
- [x] QR panel with waste type card, points card, circular arc timer, token card
- [x] Receipt screen with a manual claim code (`ECO-` + 4 random digits, stored as `claimCode` on the token)
- [x] Success overlay after QR is scanned
- [x] Firebase integration — reads/writes `qr_tokens` collection
- [x] Firestore real-time listener (`watch_token`) auto-dismisses QR on scan
- [x] QR fix — uses `INTER_NEAREST` resize to keep modules sharp and scannable
- [x] Custom YOLOv8n model `best.pt`: 20 classes, imgsz 640, 100 epochs (trained on Kaggle). Classes, validation
      results and limits: `docs/ml-model-card.md` (the first 6-class / 5-epoch prototype was replaced)
- [x] Full environment setup on new machine (Python 3.11, PyTorch CPU, all dependencies)
- [x] Flutter mobile app — users can scan QR codes and receive points
- [x] Admin web dashboard — staff can monitor waste logs and scan history

---

## What Still Needs To Be Done

- [x] **Admin Web: Manual Claim page** — staff enter claim code + username, confirm the account by email → credited like a phone scan ✅
- [x] **Connect all 3 parts to same Firebase project** — `.env.local` created with `ecoreward-bin` project config ✅
- [ ] **Test full QR flow end-to-end** — run `app.py`, scan QR with Flutter app, confirm points awarded
- [ ] **Test receipt / manual claim flow** — press R, give the username + claim code to staff, verify points credited
- [ ] **Improve the model** — mAP@50 0.776 / mAP@50-95 0.613; fix the label-set issues listed in
      `docs/ml-model-card.md` before retraining. Model files are git-ignored: keep a backup of `best.pt`

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

If no credential is found the scanner runs **OFFLINE**: a banner is shown and **no** QR codes are issued,
because a QR for a token that was never stored can never be redeemed.

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
py -3.11 app.py                                  # the bin: mode esp32 on COM3, full screen (config/kiosk_settings.json)
py -3.11 app.py --no-hardware --windowed         # the same flow with a simulated ESP32 (SIMULATION on screen)
py -3.11 app.py --mode camera --windowed         # testing without hardware: the camera sees the item leave
py -3.11 app.py --mode attendant --windowed      # testing without hardware: staff press SPACE
py -3.11 app.py --help                           # every option
```

| Mode | What confirms a deposit | Use it for |
|---|---|---|
| `esp32` (default) | the ESP32: `DONE:<n>:<grams>` after the tray tipped the item into bin n | the real bin |
| `sim` | a simulated ESP32 that speaks the same protocol over the same parser | demos, the defense, development |
| `camera` | the camera sees the item leave its view (cannot prove it went in; 15 s cool-down) | testing without hardware |
| `attendant` | a staff member presses **SPACE** | testing without hardware |

Defaults live in `config/kiosk_settings.json` and the bin rules in `config/rules.json`; command-line flags
override the settings. Older settings files still work (missing keys get defaults; `sorter` and `serial_baud`
are read as `mode` and `baud`).

### 8. Run the tests (no camera, no Firebase, no model, no ESP32 needed)
```bash
py -3.11 -I -m unittest discover -s tests -v
```
They replace Firebase, the YOLO model, the camera window and the ESP32 with fakes and exercise the real main loop.

---

## How a deposit works (modes `esp32` and `sim`)

The customer drops **one** item through the slot onto the tray. Everything after that is automatic, and no
reward code can exist before the ESP32 reports that the item left the tray (`DONE`).

1. **Ready** — "Drop one item into the slot". YOLO does **not** run; the kiosk watches the weight (`W:` every 200 ms).
2. **Weighing** — an item is present when the weight is above `min_item_g` (3 g) and stays within
   `steady_tolerance_g` (1 g) for `steady_ms` (500 ms). A hand in the slot keeps it waiting ("Checking your item…").
3. **Identifying** — YOLO runs on the `tray_roi` crop: confidence ≥ 0.30, majority of 7 frames, at most
   `identify_timeout_s` (3 s). The label maps to a category through `config/waste_map.json`.
4. **Decide** — the customer cannot reach the tray, so rejected items still go to **Residual**, without a code:
   - not recognised (no majority, timeout, or a label not in the map) → "We couldn't recognise this item. It went to Residual."
   - heavier than `max_weight_g` for that label (`rules.json`, e.g. a cup with drink left) → "Cups must be empty to earn points."
   - two or more separate objects in 3 of the 7 frames → "One item at a time, please."
5. **Sorting** — the kiosk sends the bin code (`rules.json` `bin_codes`: 1 Biodegradable, 2 Recyclable,
   3 Residual) and shows "Sorting into <Category>…". It needs `OK:<n>` within `ok_timeout_s` (3 s), then
   `DONE:<n>:<grams>` within `drop_timeout_s` (4 s). `JAM:<n>`, a wrong bin number or a timeout → **Item stuck**.
6. **Reward** (only after `DONE`):
   - earns points and Firebase is online → the token is saved in the background; the QR appears only after the
     save succeeded (20-minute expiry; leaves the screen after 3 minutes; "No phone? Claim code"; "Done"; "Claimed!").
   - earns no points → "Thank you! This item doesn't earn points." (3 s).
   - Firebase offline → "Sorted! Reward codes are offline right now." (3 s).
7. **Back to Ready** only when the scale reads below `min_item_g`; a heavier reading is a new item and is weighed.

**Item stuck** shows "Please call staff" and issues nothing; it ends when staff press **C** after clearing the
tray, or when the scale reads empty for 10 s. **Fill levels** (`FILL:` after each drop) go to Firestore
`bins/{kioskId}-bio|rec|res`; at `bin_warn_pct` (90 %) a small "bin almost full" banner appears, at
`bin_full_pct` (100 %) the kiosk **pauses** ("A bin is full. Please call staff.") until every bin reads below
100 %. If the ESP32 stops answering (unplugged, or silent for 3 s and no answer to a ping) the kiosk shows
**Out of service**, retries every 2 s, and resumes when it answers `PONG`; a deposit in progress issues no code.
**Auto-tare:** in Ready, with a steady scale reading under 5 g and nothing placed for 60 s, the kiosk sends `T`.
Weights and reject reasons go to `logs/kiosk.log` only (never to Firestore).

### Testing the whole flow in sim mode
```bash
py -3.11 app.py --no-hardware --windowed
```
1. Wait for "Drop one item into the slot" (the simulated ESP32 answers `PONG` within a second).
2. Hold an item (or a photo of one) under the webcam, then press **I**: a random 8–45 g item lands on the tray.
   With nothing recognisable under the camera you will see the "couldn't recognise → Residual" path instead.
3. Watch Checking → Sorting into <Category> → (with Firebase) the QR; scan it with the app, or tap **Done**.
4. Press **D** for diagnostics: live weight, steady flag, state, the last serial lines, fill levels and the
   camera preview with the `tray_roi` rectangle (adjust `tray_roi` in `config/kiosk_settings.json` until it
   frames the tray). The simulated fill levels rise by 1–4 % per drop; they are never written to Firestore.

---

## Controls

Customers only use the touch screen (**No phone? Claim code**, **Done**, **OK**, **Back to the QR code**);
buttons are at least 80 px tall. Staff keys:

| Key | Action |
|-----|--------|
| `D` | Diagnostics: weight, steady, state, last 20 serial lines, fill %, camera preview with `tray_roi`, model, Firestore |
| `H` | Session history (codes claimed since the kiosk started) |
| `R` | Show / hide the claim-code receipt (while a QR is shown) |
| `C` | After clearing a jammed tray by hand: back to Ready |
| `I` | Sim mode: place a simulated item on the tray |
| `SPACE` | Attendant mode: confirm the deposit (camera mode with `--debug`: manual start) |
| `Esc` | Close the overlay |
| `Q` | Quit (closing the window also quits; the camera and serial port are released and the kiosk is marked offline) |

## ESP32 protocol (modes `esp32` and `sim`)

> **Assumption — no ESP32 sketch is in this repository.** This is the protocol the kiosk implements
> (`kiosk/hardware.py`); the firmware must match it. ASCII, one message per line ending in `\n`, 115200 baud.

| Direction | Line | Meaning |
|---|---|---|
| app → ESP32 | `1` / `2` / `3` | sort into Biodegradable / Recyclable / Residual (`rules.json` `bin_codes`) |
| app → ESP32 | `T` | tare the scale |
| app → ESP32 | `P` | ping |
| ESP32 → app | `READY` | booted, chute at its home position |
| ESP32 → app | `W:<grams>` | weight, about every 200 ms (negative values after a tare are fine) |
| ESP32 → app | `OK:<n>` | the chute points at bin n |
| ESP32 → app | `DONE:<n>:<grams>` | the item left the tray into bin n (grams = weight before tipping) |
| ESP32 → app | `JAM:<n>` | the tray tipped but the weight did not drop |
| ESP32 → app | `FILL:<b>,<r>,<s>` | fill percent of bins 1, 2, 3 |
| ESP32 → app | `PONG` / `ERR:<text>` | answer to `P` / a fault |

**Firmware checklist**
1. On boot: home the chute, then send `READY`.
2. Stream `W:<grams>` about every 200 ms, always — also while sorting.
3. Answer `P` with `PONG` immediately; on `T`, tare the HX711.
4. On `n` (1, 2 or 3): move the MG996R chute to bin n's angle and wait 500 ms → send `OK:n` → tip the tray servo
   → wait until the weight is below 2 g (if that takes more than 3 s: send `JAM:n` and stop) → return the tray →
   send `DONE:n:<weight before tipping>` → measure bin n's HC-SR04 and send `FILL:<b>,<r>,<s>`.
5. Ignore a new `n` while a sort is running. Never move a servo without a command.
6. Wiring: the servos on their own 5 V supply with grounds tied to the ESP32 GND; each HC-SR04 echo pin through a
   voltage divider to 3.3 V.
7. Confirm the bin mapping **1 = Biodegradable, 2 = Recyclable, 3 = Residual** (or change `bin_codes` in `config/rules.json`).

The kiosk only marks the ESP32 ready after `PONG`, sends nothing but `P` when it (re)connects, drops commands
queued while it was unplugged, and logs the link state only when it changes. PySerial is required on the kiosk
PC: `py -3.11 -m pip install pyserial`. The `bins` writes need the rules in `firebase/firestore.rules` deployed.

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
2. The customer tells staff their **username** and the **claim code** shown on screen (`ECO-1234`)
3. Staff opens the **Admin Web Dashboard** → Manual Claim page and enters both
4. Staff confirm the account by its email (if several customers share the username, staff pick by email)
5. The site checks the code is unused and unexpired and credits the account exactly like a phone scan
   (streak multiplier, scan count, badges). If two live receipts share the 4 digits, it refuses and asks for
   the QR scan instead of guessing

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
