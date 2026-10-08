# firebase/ — Firestore Security Rules (audit C-01)

| File | What it is |
|---|---|
| `firestore.rules` | Draft rules, written from what the Flutter app, the Next.js admin and the QR flow actually do. **Not deployed.** |
| `firebase.json` | Points the Firebase CLI / emulator at the rules (no project is bound on purpose — see below). |
| `tests/rules.test.mjs` | ~60 checks that replay the real app operations as *anonymous*, *customer*, *another customer* and *admin*. |

## Run the tests (local emulator only — never touches your real Firebase project)

Needs Node 24.12+ and **Java 21+**. The default `java` on some PCs is 1.8, which is too old; on this PC use Android Studio's JDK:

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
cd firebase\tests
npm install
npm test
```

## Before you deploy these rules to the real project (do this in order)

1. **Export the rules that are live now** (Firebase Console → Firestore → Rules → copy) and save them as
   `firestore.rules.live-backup` somewhere safe. Diff them against `firestore.rules`; anything the live rules allow that the
   draft denies is a behaviour change you must understand first.
2. Run the Flutter app and the web admin against the **emulator or a separate test project** with these rules, and walk through:
   sign-up, login, scan a QR, redeem a reward, edit the profile, leaderboard, admin dashboard, reward CRUD, mark claimed, Manual Claim.
3. Only then deploy: `firebase use <project-id>` then `firebase deploy --only firestore:rules`.
   (`.firebaserc` is intentionally not committed so nobody deploys by accident.)
4. Keep the backup from step 1 so you can roll back in one paste.

## What these rules do — and do not — fix

**Fixed:** a customer making themselves admin (`role` is immutable for customers), non-admin writes to rewards / settings /
redemption status / other users, listing or enumerating QR tokens, forged user ids on logs / history / redemptions, and setting
points to an arbitrary number in one write.

**Not fixable with rules alone:** the phone still credits points itself, so a customer can raise their *own* balance in small
steps (≤ +100 per write). Closing that needs a trusted code path (Cloud Functions or a rules-validated claim batch) — audit C-01 step 3.

**Known leak kept for now:** the leaderboard requires customers to read each other's documents, which exposes e-mail addresses (audit H-13).

**Side effect to know about:** the Flutter app calls `seedRewardsIfEmpty()` at start-up, before anyone is signed in. Under these rules that
read/write is denied (silently — the app already ignores the error), so the default rewards are no longer auto-created; add rewards from the admin screens.
