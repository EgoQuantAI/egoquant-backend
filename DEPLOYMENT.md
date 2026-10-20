# Deploying EgoQuant

This is how we run the live copy. It is three services and a handful of settings. Plan an afternoon the first time; after that a deploy is a push.

## The three pieces

1. **Backend** (this repository): one Python process, FastAPI. We run it on Railway.
2. **Website** ([egoquant-frontend](https://github.com/EgoQuantAI/egoquant-frontend)): Next.js. We run it on Vercel.
3. **Accounts and saved data**: a Supabase project (Postgres with sign-in). Optional: without it the site works for visitors, nothing is saved.

Optional on top: an OpenAI key for the advisor chat, a Tiingo key for live prices, and a Sepolia test-chain key for sealing.

## 1. Backend on Railway (or any host that runs a Python process)

1. Create a service from this repository. Railway reads the `Procfile` and starts `uvicorn main:app`.
2. Set these variables. Only the first one matters for a demo:

   | Variable | Value | Why |
   |---|---|---|
   | `EGOQUANT_PRICE_MODE` | `snapshot` | Builds use the saved real prices in the repository. No live request, no key, every build takes a few seconds. |
   | `CORS_ORIGINS` | your website's URL | The API answers only to your website. |
   | `OPENAI_API_KEY` | your key | Turns on the advisor chat. Leave it empty and the chat says so politely. |
   | `SUPABASE_URL`, `SUPABASE_ANON_KEY` | from your Supabase project | Turns on accounts. The anon key only: the backend refuses to start with a service-role key, on purpose. |
   | `EGOQ_SEAL_ENABLED`, `EGOQ_SEAL_PRIVATE_KEY`, `EGOQ_SEAL_RPC_URLS` | `1`, a Sepolia test wallet with a little test ETH, and one or more RPC URLs | Turns on sealing. Test network only; the code refuses any other chain id. |

   Every other setting has a sensible default. The complete table, with the kill switches, is in [`docs/DEPLOY_ENV.md`](docs/DEPLOY_ENV.md).
3. Give it 1 GB of memory. That is what our live copy has; one portfolio is built at a time by default, so it stays within that.
4. Deploy, then open `https://your-backend/health` (should say ok) and `https://your-backend/data/status` (shows the running settings; no secrets).

## 2. Accounts on Supabase (optional)

1. Create a project. Turn on e-mail sign-in with e-mail confirmation.
2. In the SQL editor run the files in `supabase/migrations/` in file order (`001_accounts` through `006_onchain`, then `008_seal_v2`), and **`007_erase_everything` last**, again after any migration that changes "delete my data". The order matters because `007` is the final definition of that function; the step-by-step list is `docs/supabase/FROM_ONUR_runbook.md`.
3. Check the row-level security proofs: run `supabase/tests/rls_isolation.sql` and `supabase/tests/rls_onchain.sql`; every line should say PASS.
4. Put the project URL and the anon key into the backend's variables (above). The website needs nothing from Supabase: it only talks to the backend.

A note from experience: Supabase's shared e-mail sender allows only a few confirmation e-mails an hour. For a demo day, make the demo accounts in advance or add your own SMTP sender.

## 3. Website on Vercel

1. Import the frontend repository. It is a standard Next.js project.
2. Set `BACKEND_URL` to your backend's URL. That is the only setting it needs.
3. Deploy. The website never talks to the backend from the browser directly; its own server routes add the signed-in person's token and pass the call on.

## 4. Check it

From any machine with Python:

```bash
python eval/deploy_check.py https://your-backend
```

It is read-only. It checks the status page, the security headers, CORS, builds three demo investors and compares the results with the committed demo pack. Ours passes 71 of 71.

## If something breaks

- The chat is down: unset `OPENAI_API_KEY` or leave it; reports, limits and trade checks never depend on it.
- The price feed is slow: builds switch to the saved prices by themselves after 15 seconds and say so; `EGOQUANT_PRICE_MODE=snapshot` avoids the wait altogether.
- Too many people building at once: `EGOQUANT_MAX_CONCURRENT_BUILDS` (default 1) is the knob; everyone else waits their turn or is asked to try again.
- Anything else: redeploy the previous version. Nothing in the backend keeps state between deploys except a price cache.
