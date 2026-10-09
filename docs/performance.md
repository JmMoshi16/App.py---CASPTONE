# Performance — budgets, measurements, before/after

Measured 2026-10-09 on the development PC (Windows 11, CPU only). Re-run the same tools on the real kiosk PC
and with production data before relying on these numbers.

## Budgets

| Budget | Target | Result | Status |
|---|---|---|---|
| Kiosk camera view | ≥ 10 FPS | 24.5 FPS (inference + drawing per frame) | ✅ |
| QR shown after SPACE | < 1 s | 36 ms token write (p95 57 ms) + one network round trip (typically 50–300 ms) | ✅ |
| Reads when an admin opens the dashboard | < 2,000 | 1,839 on the load data set (was 5,803) | ✅ |
| Web first-load JavaScript per page | ≤ 350 kB | 213–333 kB (chart pages ~330 kB) | ✅ |
| Kiosk start-up splash | < 10 s | ≈ 0.5 s of animation (was ≈ 81 s, Phase 2) | ✅ |

## Kiosk (`py -3.11 tools/bench_kiosk.py`)

Real `best.pt` on a synthetic 640×480 frame, 60 runs each.

| Step | Median ms | p95 ms |
|---|---:|---:|
| YOLO, native 640, conf 0.15 (as shipped) | 35.8 | 39.5 |
| YOLO, imgsz 480 | 25.2 | 30.6 |
| YOLO, imgsz 320 | 16.9 | 20.7 |
| HUD resize + drawing | 5.1 | 5.7 |

UI rate with inference on every frame: **24.5 FPS**. A worker thread would give ~197 FPS of drawing but
detections would still update ~28×/s — not needed at this speed, so the loop stays simple. If the kiosk PC is
slower than 10 FPS, the cheapest fix is `imgsz=480` (−30 %, accuracy to be confirmed with `tools/eval_model.py`).

## Admin web: Firestore reads (`E2E_SEED=load npm run perf`)

Load data set in the emulator: 500 customers, 5,000 waste logs and 5,000 scans over 90 days, 300 redemptions.
Reads are counted by the test build (one per document delivered; aggregations ≈ 1 per 1,000 documents counted).

| Page | Before: opened from the dashboard | After: opened directly | After: opened from the dashboard |
|---|---:|---:|---:|
| Dashboard | 5,803 | 1,839 | 1,839 |
| Analytics | 0† | 1,841 | 5 |
| Users | 0† | 2,287 | 501 |
| Scan History | 5,003 | 225 | 225 |
| Leaderboard | 50 | 50 | 0 |
| Rewards | 1 | 301 | 301 |
| **Whole sidebar tour** | **≈ 10,860** | — | **≈ 2,870** (−74 %) |

† Shared listeners (Phase 6) already made these free after the dashboard; opened directly they cost
≈ 5,800 / 5,500 reads before (every log + customer + redemption). Time to content: 1.5–2.6 s per page after.

**What changed:** the last 31 days of logs are shared by Dashboard, Analytics and Users (all-time logs load only
when "All" is chosen); customer and redemption totals use server-side `count()`/`sum()`; top customers come from
the 50-row leaderboard; per-customer totals use `users.scanCount`; one customer's history loads when their panel
opens; Scan History loads 200 rows at a time with totals by `count()`.

**Prerequisite:** run `tools/migrate_timestamps.py --apply` before deploying — date-range queries skip documents
whose timestamp is still an ISO string.

**Scaling limit:** dashboard reads ≈ scans in the last 31 days + ~10. Above ~60 scans per day this passes 2,000
again; the fix then is daily aggregate documents written by a Cloud Function (Blaze plan).

## Phone app: reads per scan (from the code; not measured on a device)

Claim ≈ 5 reads (token, user, profile, scan-history lookups), then the home screen reloads the customer's
whole history, all rewards and their redemptions: **≈ 6 + history + rewards + redemptions** per scan — about
230 reads for a customer with 200 scans. Recommended next step: keep the home tab's data between scans and
load history in pages (needs a device to verify; `flutter test` cannot run on the development PC).
