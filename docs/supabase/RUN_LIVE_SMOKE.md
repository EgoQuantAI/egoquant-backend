# Running the live smoke test (about 10 minutes, by a human)

The smoke test proves, on the REAL Supabase project and the DEPLOYED backend, that one user cannot see another's data and that "delete my data" works.
It needs two accounts. Nobody writes their passwords in a file, a commit, a log or a chat: you type them into your own shell window only.

## 1. Two accounts that already exist (the dashboard's "Add user" does not work for this)
Our database trigger takes the **username** from the sign-up form (`handle_new_user` in `supabase/migrations/001_accounts.sql`), so a user created in the dashboard without one is refused.
Use two accounts that were created through the **site's sign-up page** and confirmed: for example the two demo accounts (they are only used to log in; the test erases their saved reports, imported trades and consents,
never the accounts themselves). If you prefer separate throw-away accounts, sign them up on the site (each one sends one confirmation e-mail, so use at most two) and delete them afterwards from the account page ("Delete account").
(Do not switch "Confirm email" off.)

## 2. Run it (PowerShell, from the backend folder)
```powershell
cd C:\Users\NYUAD\Downloads\FinTechaton\fintechaton-backend
$env:BACKEND_URL = "https://web-production-59960.up.railway.app"
$env:SMOKE_A_EMAIL = "<e-mail of account A>"; $env:SMOKE_A_PASSWORD = "<its password>"
$env:SMOKE_B_EMAIL = "<e-mail of account B>"; $env:SMOKE_B_PASSWORD = "<its password>"
.venv\Scripts\python supabase\tests\live_smoke.py
```
`SUPABASE_URL` and `SUPABASE_ANON_KEY` are read from the backend's own `.env` (git-ignored). The script prints only PASS/FAIL lines: no key, token, e-mail or password.
Expected: every line `PASS`, last line "N passed, 0 failed", exit code 0. It keeps the two accounts (it never deletes an account it did not create) but erases their data.

## 3. Clean up
If you used throw-away accounts, delete them from the account page. Close the shell window so that the variables disappear.
Paste the PASS/FAIL lines (they contain nothing secret) into `docs/supabase/SUPABASE_LOG.md`.

## If a line fails
Copy the FAIL line (not the whole screen) into your report. Common causes: the backend is an old deploy (check `/data/status`), a migration is missing (`docs/supabase/SETUP_RUNBOOK.md`), or the two users were created without Auto Confirm.
