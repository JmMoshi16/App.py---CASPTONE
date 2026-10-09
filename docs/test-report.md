# Test report — ECOReward Bin

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
