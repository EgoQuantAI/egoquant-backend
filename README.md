# EgoQuant (backend)

EgoQuant is a wealth-advisory agent we built for the 2026 Shenzhen FinTechathon (WeBank International Track, Topic B). You answer a proper risk questionnaire, play a short game about risk, say what you'd like to hold, and you get a portfolio that fits you, with every number explained and checked by a second, independent piece of code. An AI assistant explains the result and can try "what if" questions, but it never decides anything. If you confirm a plan, a fingerprint of it goes on a public test blockchain so you can prove later that nobody changed it.

This repository is the engine and the API (Python, FastAPI). The website lives in [egoquant-frontend](https://github.com/EgoQuantAI/egoquant-frontend).

This is a snapshot of the version we submitted on 20 October 2026 (tag `submission-20oct`), in one commit. Our day-to-day development history stays in a private repository.

## Try it without installing anything

- Website: https://fintechaton-frontend.vercel.app
- API with interactive docs: https://web-production-59960.up.railway.app/docs

Two minutes, no account: open **Verify a report** in the sidebar, press **Use the demo report**, then **Try a tampered copy**. The first checks a real sealed report against the Sepolia test chain; the second shows what happens when one number was changed.

## Where to read first

Everything we'd show a judge is in [`docs/judges/`](docs/judges/README.md): a summary, the security and compliance report, how each method works, what we tested, a user guide with screenshots, the limits we know about, and the questions we expect. The submission package itself (PDFs, slides, evidence logs) is in [`submission/`](submission/README.md).

## Run it on your machine

You need Python 3.11 or newer. Nothing else: the repository carries a saved snapshot of real market prices, so no API key is required to build portfolios.

```bash
git clone https://github.com/EgoQuantAI/egoquant-backend.git
cd egoquant-backend
python -m venv .venv
.venv/Scripts/activate        # on macOS or Linux: source .venv/bin/activate
pip install -e ".[dev]"
set EGOQUANT_PRICE_MODE=snapshot    # on macOS or Linux: export EGOQUANT_PRICE_MODE=snapshot
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs and you can call every endpoint from the browser. `GET /questionnaire` to read the questions, `POST /profile` to score answers, `POST /portfolio` to build a portfolio.

Things that are off until you give them a key, and work fine without one:

| Feature | What you need | Without it |
|---|---|---|
| The advisor chat | `OPENAI_API_KEY` | The chat answers "temporarily unavailable"; reports, limits and trade checks still work |
| Live prices | `TIINGO_API_KEY` and `EGOQUANT_PRICE_MODE=live` | The saved snapshot of real prices (to 16 October 2026) is used and labelled |
| Accounts and saved reports | a Supabase project (`SUPABASE_URL`, `SUPABASE_ANON_KEY`) | Everything works as a visitor; nothing is saved |
| Sealing on the test chain | `EGOQ_SEAL_ENABLED=1`, `EGOQ_SEAL_PRIVATE_KEY` and `EGOQ_SEAL_RPC_URLS` | Plans can be confirmed; no fingerprint is published |

The full list of settings, with a sentence for each, is in [`docs/DEPLOY_ENV.md`](docs/DEPLOY_ENV.md).

## Run the tests

```bash
pytest -q -m "not live"
```

3,554 tests, no network, no AI; roughly 35 minutes. On Windows the numerical libraries sometimes crash a long test process at random (we explain this honestly in `docs/judges/04_TESTING_AND_EVIDENCE.md`, section 9); if that happens, run them folder by folder with `python eval/run_tests_per_file.py . results.txt --dir tests/system`.

To check a deployed copy without touching anything on it:

```bash
python eval/deploy_check.py https://your-backend-url
```

## Deploy it

See [`DEPLOYMENT.md`](DEPLOYMENT.md). Short version: it is one Python process; we run it on Railway, the website on Vercel, accounts on Supabase, and it took us an afternoon.

## What it is not

It places no orders and moves no money. The category limits are our own placeholders, not a bank's policy. Every chance it shows is a simulation on past prices, not a forecast. We list what we know is weak in `docs/judges/06_LIMITATIONS_AND_ROADMAP.md` and `docs/SECURITY.md`.

## The team

Savas, Umut Bideci, Aslan and Zerya, with a few Claude sessions doing the typing. Questions: open an issue here.

The long technical README of the development repository is [README_DETAILED.md](README_DETAILED.md).
