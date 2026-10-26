# Privacy notice — ECOReward Bin

> **For the team:** fill in the two placeholders in square brackets before showing this to customers, and
> have it checked by whoever advises the cafe on data protection (in the Philippines that is the Data
> Privacy Act of 2012). The facts below describe what the code actually does.

ECOReward Bin rewards you with points when you sort your waste at the cafe's smart bin.
This notice explains what information we keep, why, and how to have it deleted.

## What we collect

| Information | When | Why |
|---|---|---|
| Your e-mail address and password | When you register | So you can sign in. Your password is handled by Google Firebase Authentication; we never see it. |
| Your username | When you register or change it | Shown on the leaderboard |
| Your profile photo (optional) | When you add one in Edit profile | Shown to you in the app. Never on the leaderboard; you can remove it any time |
| Each scan: waste category, time, points earned | When you scan a bin QR code | To credit your points, streaks and badges, and show your history |
| Rewards you redeem and their codes | When you redeem | So cafe staff can hand the reward over |

**The kiosk camera** looks at the item you hold up to decide its category. Camera images are processed on the
kiosk and **are not saved or uploaded**. Only the result (for example "can", "Recyclable", 92 %) is recorded,
together with the time and the kiosk that made it.

We do **not** use advertising, analytics or tracking tools in the app or the admin site.

## Who can see it

- **You** — your profile, points, history and redemptions.
- **Other customers** — only your **username, points and badges** on the leaderboard. Never your e-mail or photo.
- **Cafe staff with admin access** — your account, scans and redemptions, to run the reward programme.

Your data is stored in Google Firebase (Cloud Firestore and Authentication) for the cafe's project.

## How long we keep it

As long as your account exists. If you ask us to delete it, we delete your account and everything linked to it
(profile, scan history, redemptions) — see below.

## Deleting your data

Ask cafe staff, or e-mail **[cafe contact e-mail]**. We will confirm it is your account and delete your data
within **[number] days**. Points and unclaimed rewards are lost when the account is deleted.

*(Staff procedure: an admin runs `tools/delete_user_data.py <your e-mail>` — first as a dry run to see what will
be removed, then with `--apply`. It deletes the sign-in account, your `users` and `profiles` records, and your
scan logs, scan history and redemptions.)*

## Questions

Ask at the counter or e-mail **[cafe contact e-mail]**.
