# Threat model — ECOReward Bin

Scope: the kiosk (`app.py`), the customer/admin Android app, the admin web console, and the shared Firebase
project. This document states who can do what, how that is enforced, how it is tested, and what is **not**
protected yet. It is written so a reviewer can check each claim against the code and the tests.

## What we protect

| Asset | Why it matters |
|---|---|
| Customer accounts (e-mail, username) | Personal data |
| Point balances, lifetime points, badges | They are redeemed for real rewards (coffee, discounts) |
| Rewards, stock, redemption codes | Real cost to the cafe |
| Admin rights | Control over all of the above |
| The kiosk's Firebase access | A public, unattended PC |

## Who can do what

All access goes through **Firestore Security Rules** (`firebase/firestore.rules`), except the maintenance
tools that an admin runs with the service-account key on their own computer.

| Identity | How it is recognised | Allowed | Not allowed |
|---|---|---|---|
| Anonymous visitor | not signed in | nothing | everything |
| Customer | signed in, no claims | own `users` doc (bounded fields), public `profiles`, claim **one** unused, unexpired QR token, own logs/redemptions, read rewards | other customers' `users` (e-mails), changing `role`/e-mail, raising points by more than +100 per write, lowering lifetime points, editing rewards or redemption status, listing tokens |
| Admin | custom claim `admin: true`, or `users.role == 'admin'` | manage rewards, redemptions, settings, read all collections | — (trusted role) |
| Kiosk | custom claim `kiosk: true` (its own Auth user) | create a QR token of the exact kiosk shape (unused, known category, 4-digit code, server-timestamped, expires ≤ 30 min), read a token by id, write its **own** heartbeat, add detections in its own name | read any user, profile, log, redemption, settings; claim tokens; act as a customer; edit or list tokens |
| Maintenance tools | service-account key on an admin computer | everything (Admin SDK bypasses rules) | — (kept off the kiosk) |

Claims can only be set with the Admin SDK (`tools/set_claims.py`). A customer cannot grant themselves a
claim, and cannot change `role`.

**Evidence:** `firebase/tests/rules.test.mjs` — 77 emulator checks covering each identity above, including
scripted escalation attempts (self-promotion, forged profiles, forged tokens, kiosk reading users).
`tests/test_kiosk_firestore.py` runs the real kiosk client against the real rules in the emulator.

## Trust boundaries and controls

1. **Phone/web → Firestore.** Clients are untrusted; the rules decide. Field whitelists, bounded point
   changes, `profiles` must equal the owner's `users` document after the same write, one claim per token,
   server-time expiry for Timestamp tokens.
2. **Kiosk → Firestore.** The kiosk uses a least-privilege identity over the REST API (`kiosk_firestore.py`);
   its config holds a public API key and a revocable refresh token, never a password or private key.
   Revocation: `tools/set_claims.py revoke-kiosk <name>`.
3. **Admin computer → Admin SDK.** The service-account key is used only by the maintenance tools and must live
   outside the repository (`~/.ecoreward/kiosk-sa.json`).
4. **Web console → browser.** Security headers (no framing, `nosniff`, referrer and permissions policy,
   `noindex`); a full Content-Security-Policy runs in report-only mode until verified.
5. **Repository.** `.gitignore` covers credential names; gitleaks runs as a pre-commit hook and in CI
   (`.github/workflows/secret-scan.yml`). A full-history scan of all three repositories found no private keys,
   service-account files or tokens.

## Known limitations (accepted for now)

| # | Limitation | Impact | Mitigation now | Proper fix |
|---|---|---|---|---|
| L1 | The **phone credits points itself** (no server code). Rules bound each write but cannot prove a QR was genuine. | A determined customer can raise their own balance in steps of ≤ +100 | Bounds, lifetime monotonic, one claim per token, audit trail in `waste_logs`/`qr_scan_history` | Move claim/redeem into Cloud Functions (needs the Blaze plan) |
| L2 | A stolen kiosk identity can still **create valid QR tokens** until it is revoked. | Points can be minted, ≤ 30 per scan with the streak bonus, each token claimable once | Token shape and lifetime enforced by rules; detections log every token; revoke in one command | Server-side issuing tied to bin sensor events |
| L3 | **Legacy kiosk mode** with the service-account key still works if `kiosk.json` is missing. | Full database access from the kiosk PC | Loud `[SECURITY]` warning at start-up | Remove the key from every kiosk PC, then delete the legacy branch |
| L4 | **App Check is not enabled** and the API keys are not yet restricted. | Scripts using a valid customer login are not distinguished from the real app | Rules still apply to every request | Console steps below |
| L5 | Manual-claim codes are 4 digits. | Guessable by an attacker with admin access only (it is an admin screen) | Admin-only, single use, expiry | Longer codes if staff accept them |
| L6 | Web dependencies with advisories that need **major** upgrades (Next 16, Tailwind 4, Firebase 11+). | Mostly build-time/server features this static admin site does not use | Non-breaking fixes applied | Dependency phase (roadmap Phase 11) |
| L7 | The deployed rules in the Firebase console have not been compared with this repository. | Unknown until checked | — | `firebase/README.md`: back up, diff, deploy |

## Console actions still required (only the project owner can do these)

1. **Deploy the rules** in `firebase/firestore.rules` (after backing up the live ones) and the indexes.
2. **Rotate the service-account key** and delete the old key ID (Google Cloud → IAM → Service accounts → Keys).
3. **Restrict API keys** (Google Cloud → APIs & Services → Credentials):
   - Android key: *Android apps* → package `com.example.ecoreward_bin` + the release SHA-1 (from the new keystore).
   - Browser key: *HTTP referrers* → the admin site's domain(s) and `localhost` for development.
   - Both: *API restrictions* → Identity Toolkit, Token Service, Cloud Firestore (and Firebase Installations if used).
4. **App Check**: register the Android app with Play Integrity and the web app with reCAPTCHA Enterprise,
   watch the metrics for a week in "unenforced" mode, then enforce for Firestore and Authentication.
   (Each client also needs the App Check SDK added — a small code change to do together with this step.)
5. **Kiosk identity**: run `tools/set_claims.py create-kiosk`, install `kiosk.json` on the kiosk, remove the
   service-account key from the kiosk PC.

## Reporting a problem

Tell the project team privately (not in a public issue). Include what you saw and how to reproduce it.
