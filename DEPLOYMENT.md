# Deploying EgoQuant

This is how we run the live copy (https://www.egoquant.app). It is three services and a handful of settings. Plan an afternoon the first time; after that a deploy is a push.

## The three pieces

1. **Backend** (this repository): one Python process, FastAPI. We run it on Railway.
2. **Website** ([egoquant-frontend](https://github.com/EgoQuantAI/egoquant-frontend)): Next.js. We run it on Vercel.
3. **Accounts and saved data**: a Supabase project (Postgres with sign-in). Optional: without it the site works for visitors and nothing is saved.

Optional on top: an OpenAI key for the advisor chat, a Tiingo key for live prices, and a Sepolia test-chain key for sealing.

## 1. Backend on Railway (or any host that runs a Python process)

1. Create a service from this repository. Railway reads `.python-version` (3.14) and the `Procfile`, which starts `uvicorn main:app`.
2. Set the variables. Only the first one matters for a demo:

   | Variable | Value | Why |
   |---|---|---|
   | `EGOQUANT_PRICE_MODE` | `snapshot` | Builds use the saved real prices in the repository: no live request, no key, a few seconds per build. |
   | `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`) | your key | Turns on the advisor chat. Without it the chat says it is unavailable; nothing else depends on it. |
   | `SUPABASE_URL`, `SUPABASE_ANON_KEY` | from your Supabase project (section 2) | Turns on accounts. Use the anon (publishable) key only: the backend refuses to start with a service-role key, on purpose. `SUPABASE_KEY` is accepted as the same thing. |
   | `EGOQ_SEAL_ENABLED`, `EGOQ_SEAL_PRIVATE_KEY`, `EGOQ_SEAL_RPC_URLS` | `1`, a Sepolia test wallet with a little test ETH, one or more RPC URLs | Turns on sealing. Test network only; any other chain id is refused. |
   | `CORS_ORIGINS` | your website's URL | Optional. The website calls the API from its own server, so CORS only matters if a browser calls the API directly. |

   Every other setting has a sensible default. The complete table, with the kill switches, is in [`docs/DEPLOY_ENV.md`](docs/DEPLOY_ENV.md). Keys go into the host's variables, never into the repository.
3. Give it 1 GB of memory. One portfolio is built at a time by default (`EGOQUANT_MAX_CONCURRENT_BUILDS`), so it stays within that.
4. Deploy, then open `https://your-backend/health` (answers `{"status":"healthy"}`) and `https://your-backend/data/status` (the running settings, no secrets).

## 2. Accounts on Supabase (optional)

1. Create a project. Under Authentication, keep only e-mail sign-in, with e-mail confirmation on, and set a minimum password length (we use 10).
2. In the SQL editor run the files in `supabase/migrations/` in file order (`001_accounts` to `006_onchain`, then `008_seal_v2`), and **`007_erase_everything` last**, again after any migration that changes "delete my data": `007` is the final definition of that function. The step-by-step list is [`docs/supabase/FROM_ONUR_runbook.md`](docs/supabase/FROM_ONUR_runbook.md).
3. Run `supabase/tests/rls_isolation.sql` and `supabase/tests/rls_onchain.sql`; every line should say PASS.
4. Under Authentication, URL Configuration: set the **Site URL** to your website (ours: `https://www.egoquant.app`) and add `https://your-website/**` to the **Redirect URLs**. Confirmation and password-reset links come back to the website's root; a website address that is not on this list sends people to the Site URL instead.
5. The project URL and the anon key go into **both** the backend (section 1) and the website (section 3).

From experience: Supabase's shared e-mail sender allows only a few e-mails an hour. For a demo day, create the demo accounts in advance or add your own SMTP sender.

## 3. Website on Vercel

1. Import the frontend repository. It is a standard Next.js project (Node.js 22).
2. Set the variables for Production (and Preview if you use preview links):

   | Variable | Value |
   |---|---|
   | `BACKEND_URL` | your backend's URL, for example `https://web-production-59960.up.railway.app` |
   | `NEXT_PUBLIC_SUPABASE_URL` | the Supabase project URL (only with accounts) |
   | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the Supabase anon key (only with accounts) |

   Sign-in runs in the website's own server routes, which talk to Supabase and keep the session in HttpOnly cookies; that is why the website needs the Supabase URL and anon key too. Without them the website works for visitors and says that accounts are off. Without `BACKEND_URL` it falls back to the address written in `src/lib/backend.ts`.
3. Deploy. A changed variable only reaches a new build, so redeploy after changing one.

## 4. Check it

```bash
python eval/deploy_check.py https://your-backend
```

It is read-only: it checks the status page, the security headers, CORS, builds three demo investors and compares the results with the committed demo pack, and runs trade checks. Ours passes 71 of 71. Then open the website, sign up with a real mailbox, confirm the e-mail and build a portfolio.

## If something breaks

- The chat is down: reports, limits and trade checks never depend on it; the chat says so.
- The price feed is slow: builds switch to the saved prices by themselves after 15 seconds and say so; `EGOQUANT_PRICE_MODE=snapshot` avoids the wait altogether.
- Too many people building at once: `EGOQUANT_MAX_CONCURRENT_BUILDS` (default 1) is the knob; others wait their turn or are asked to try again.
- Sign-up e-mails lead to the wrong site: check the Site URL and Redirect URLs in Supabase (section 2, step 4).
- Anything else: redeploy the previous version. The backend keeps no state between deploys except a price cache.
