# Test plan and traceability — ECOReward Bin

Every automated suite runs without the real Firebase project: the kiosk tests use fakes, everything that
needs a database uses the local Firebase emulators. Results of the latest run: `docs/test-report.md`.

## Suites and how to run them

| Suite | Repo | Command | What it covers |
|---|---|---|---|
| Kiosk unit | kiosk | `py -3.11 -I -m unittest discover -s tests -v` | Real `main()` loop with fake camera/model/Firestore; tools; kiosk REST client |
| Rules + races | kiosk | `cd firebase/tests && npm test` | Every role against `firestore.rules`; concurrent claim / redeem / manual claim |
| Emulator end-to-end | kiosk | `cd firebase/tests && npx firebase emulators:exec --config ../firebase.json --only firestore --project demo-ecoreward "py -3.11 -I -m unittest discover -s ../../tests -v"` | Kiosk client vs real rules; **golden path** kiosk → phone → kiosk → admin |
| Break-and-restore | kiosk | `py -3.11 tools/break_restore.py` | Re-introduces C-01…C-05 and H-02 in temporary copies; each must make a test fail |
| Web unit | web | `npm test` | Safe dates, CSV, shared listeners, the C-05 load-timeout hook (React + fake timers) |
| Web static | web | `npm run typecheck && npm run lint` | TypeScript strict, ESLint (no unused, no `any`) |
| Web browser smoke | web | `npm run e2e` | Playwright against Auth + Firestore emulators |
| Flutter static | app | `flutter analyze` | Must report "No issues found" |
| Flutter rules (pure Dart) | app | `dart test test/gamification_test.dart test/auth_messages_test.dart` | Streak, multiplier, credited points, weekly goal, auth messages |
| Flutter tests | app | `flutter test` | Rank, level, badges, models (runs in CI; blocked on the development PC by Windows Application Control) |
| ML evaluation | kiosk | `py -3.11 tools/eval_model.py --set <frozen kiosk test set>` | Needs the kiosk-camera photo set (not collected yet) |

Rules/emulator suites need Node 24+ and Java 21+ (`JAVA_HOME`). CI: `.github/workflows/ci.yml` in each repo.
On Windows keep the checkouts at a short path (e.g. `C:\dev\…`): `dart test` starts its compiler through a long
relative path and fails with "The system cannot find the file specified" beyond Windows' 260-character limit.

## Traceability: every Critical / High item

✅ automated · 🧪 automated, emulator · 🖐 manual / hardware · ⏳ not testable yet

| Item | Guarded by | Type |
|---|---|---|
| **C-01** client-written role/points | `rules.test.mjs`: "users - updating", "end-to-end story", "admin custom claim"; break-and-restore C-01 | 🧪 |
| **C-02** full-access key on kiosk | `rules.test.mjs` "kiosk identity"; `test_kiosk_firestore.py` EmulatorTests; golden path (kiosk cannot read users); `CredentialLookupTests`; break-and-restore C-02 | 🧪 ✅ |
| **C-03** offline kiosk issues QRs | `test_kiosk_phase1.py` `OfflineTests`; break-and-restore C-03 | ✅ |
| **C-04** scan never detected | `ScanDetectionTests` (4 cases); `GoldenPathEmulatorTest`; break-and-restore C-04 | ✅ 🧪 |
| **C-05** "Failed to load" after 15 s | `lib/useLoadTimeout.test.ts` (3 cases); e2e 20 s soak; break-and-restore C-05 | ✅ 🧪 |
| **H-01** whole-document user rewrite | `rules.test.mjs` "H-01 payload", "older app version…", "username change"; Flutter device check | 🧪 🖐 |
| **H-02** token reused | `rules.test.mjs` qr_tokens block, "two phones scan the same QR at the same moment"; golden path (second phone refused); break-and-restore H-02 | 🧪 |
| **H-03** QR without a detection | `DetectionGuardTests` | ✅ |
| **H-04** model not reproducible | `WasteMapTests.test_every_class_of_the_shipped_model_is_mapped`; model card | ✅ 🖐 |
| **H-05** ML evaluation | `test_eval_model.py` (scoring); real numbers need the photo set | ✅ ⏳ |
| **H-06** manual-claim race | `rules.test.mjs` "two staff enter the same claim code at once" | 🧪 |
| **H-07** timestamp drift | `test_kiosk_firestore.py` timestamp round trip; web `lib.test.ts` dates; `rank_badges_test.dart` model fallbacks | ✅ |
| **H-08** Scan History double count | e2e: two scans → exactly two rows | 🧪 |
| **H-09** fabricated UI | Review: invented trend %s and health bars removed (Phase 6) | 🖐 |
| **H-10** half-registered account | `rules.test.mjs` "registration self-heal"; Flutter login on a device | 🧪 🖐 |
| **H-11** Flutter error UI, camera denied | Device: deny camera → permission screen + retry; admin dashboard offline → Retry | 🖐 |
| **H-12** indexes | `firestore.indexes.json` reviewed; console shows them "Enabled" after deploy | 🖐 |
| **H-13** customer e-mails readable | `rules.test.mjs` "users - reading (private)", "profiles"; golden path | 🧪 |
| **H-14** kiosk runtime fragility | `ScanDetectionTests.test_leftover_scan_signal…` (failed write → no QR); camera unplug on hardware | ✅ 🖐 |
| **H-15** version control hygiene | gitleaks pre-commit + CI `secret-scan.yml` | ✅ |
| **H-16** no tests / CI | this plan; `.github/workflows/ci.yml` ×3 | ✅ |
| **H-17** documentation | Phase 10 | ⏳ |
| **H-18** splash ≈ 80 s | `SplashPerformanceTest` | ✅ |

Selected Medium items: M-03 `sharedSubscription.test.ts`; M-04 `lib.test.ts` CSV; M-05 e2e headers; M-06 `lib.test.ts` dates;
M-07 e2e leaderboard; M-11 `gamification_test.dart`; M-17 `auth_messages_test.dart` + e2e wrong password;
M-20 `OneVotePerFrameTest`; M-21 `DetectionLogTest`; M-22 `WasteMapTests`; M-24 `ConfigParityTest`.

## Manual checks before a demo (hardware / accounts)

1. Kiosk with the real camera and `kiosk.json`: start-up line says "kiosk identity"; hold an item, SPACE, scan with a phone → success screen within ~2 s.
2. Phone: deny camera permission → permission screen; allow → scanning works.
3. Same QR on a second phone → "Already Used".
4. Web: sign in as admin, leave the dashboard open 5 minutes → no "Failed to load"; Scan History count = scans made.
5. Redeem a reward on the phone, hand it out on the web ("Mark as claimed"), then try the same code again.
