# Test report — ECOReward Bin

## 2026-10-10 — kiosk redesign and reliability pass (kiosk repository only)

**Machine:** the same development PC (Windows 11, Python 3.11, OpenCV 5.0.0, Node 24, Java 25 from Android Studio's
JBR). **Method:** one run in the development checkout (not a fresh clone). Commits: `c16eca0` … `9f2361a`.

| Suite | Result |
|---|---|
| Kiosk unit + product tests (fakes, no Firebase) | 94 run, OK (5 skipped: emulator-only) |
| Kiosk tests inside the Firestore emulator (golden path with the real rules, kiosk client, `bins`) | 94 run, OK, 0 skipped |
| Firestore rules + races (emulator) | 84 / 84 pass (2 new: kiosk `bins` writes; ISO-string token dates rejected) |
| Break-and-restore (`tools/break_restore.py`) | 10 / 10 caught (new: K-01 ISO dates, K-06 stale servo command, K-07 UI-thread write, K-10 leaking poller) |
| `tools/bench_kiosk.py` (real `best.pt`) | inference 38.8 ms + full-frame UI 6.9 ms → 21.8 FPS (budget ≥ 10) |
| Real OpenCV window, offline, synthetic camera, real `best.pt`, simulated sorter | 140 frames / 10.7 s incl. start-up, window visible throughout, staff keys and clean shutdown OK |

What the new tests cover: two simulated ESP32 deposits in a row through the real `main()`; a 0.6 s token write
while the screen keeps drawing (≥ 10 frames); the watcher stopping after a claim and after a walk-away; camera
loss and re-open; inference and drawing exceptions; a touch tap opening and closing the claim-code receipt; fill
levels written in the web's `BinStatus` shape (never from the simulator); every screen at 1024×600, 1280×720 and
1920×1080; the on-screen QR decoded by OpenCV to the exact payload at all three sizes; WCAG AA contrast of every
text/background pair; touch targets ≥ 44 px on the 7" display; the token document contract.

**Not verified here (needs hardware or the live project):** the real webcam, an ESP32 running the assumed
protocol, a touch display, the window in fullscreen on the kiosk PC, a phone scan against the deployed rules.

---

## 2026-10-09 — Phase 8 (all three repositories)

**Date:** 2026-10-09 · **Machine:** Windows 11, Python 3.11, Node 24.20, Java 21 (Android Studio JBR), Flutter 3.47.2
**Method:** fresh `git clone` of all three repositories, every suite run **twice** in a row (audit Phase 8).
Commits tested: kiosk `28ed742`, web `af2b3ce`, app `15cfc94`. How to run each suite: `docs/test-plan.md`.

## Results (both rounds identical)

| Suite | Round 1 | Round 2 |
|---|---|---|
| Kiosk unit (fakes, no Firebase) | 48 run, OK (6 skipped*) | 48 run, OK (6 skipped*) |
| Firestore rules + races (emulator) | 82 / 82 pass | 82 / 82 pass |
| Kiosk client + golden path (emulator) | 48 run, OK (2 skipped*) | 48 run, OK (2 skipped*) |
| Web typecheck / lint | 0 errors / clean | 0 errors / clean |
| Web unit | 11 / 11 pass | 11 / 11 pass |
| Web browser smoke (Playwright + emulators) | 4 / 4 pass (49.7 s, cold start) | 4 / 4 pass (27.6 s) |
| Flutter analyze | No issues found | No issues found |
| Dart rule tests | 18 / 18 pass | 18 / 18 pass |

\* Skips in a fresh clone are expected: `best.pt` is not stored in git (the real-model label check skips),
the cross-repo `rules.json` parity check needs the sibling checkouts, and the emulator-only tests skip in the
plain run. All of them pass in the development checkout and inside the emulator.

## Break-and-restore (`tools/break_restore.py`)

Each Critical bug was re-introduced in a temporary copy; the matching suite must pass unbroken and fail broken.

| Item | Re-introduced bug | Failing tests unbroken → broken | Result |
|---|---|---|---|
| C-03 | Kiosk issues a QR while Firebase is offline | 0 → 1 | caught |
| C-04 | Kiosk never notices the phone's scan | 0 → 2 | caught |
| C-05 | Web page shows "Failed to load" after 15 s although data arrived | 0 → 2 | caught |
| C-01 | A customer can change their own role to admin | 0 → 3 | caught |
| C-02 | The kiosk identity can read customers' user documents | 0 → 1 | caught |
| H-02 | A QR token can be claimed twice | 0 → 1 | caught |

## Defects found by the new tests (fixed)

1. **Admin sign-in race (web).** Right after signing in, the auth context briefly reported "no user, not
   loading", so the route guard sent the admin back to the login page while the login page sent them forward.
   Found by the browser smoke test (intermittent). Fixed in web `af2b3ce`; 5 consecutive runs then passed.
2. **Test-only:** Playwright's default 5 s wait was shorter than a cold emulator's first query; aligned with the
   app's own 15 s load timeout.

## Not covered automatically

- `flutter test` (rank/level/badge/model tests) cannot run on the development PC — Windows Application Control
  blocks `flutter_tester.exe`. It is in the app's CI workflow.
- CI workflows have not run on GitHub yet (nothing has been pushed).
- Hardware and account checks: real camera and phone scan, TalkBack, camera-denied flow, deployed rules and
  indexes, ML accuracy on a kiosk-camera photo set — see the manual list in `docs/test-plan.md`.
