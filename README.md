# EgoQuant (backend)

EgoQuant is a wealth-advisory agent we built for the 2026 Shenzhen FinTechathon (WeBank International Track, Topic B). You answer a proper risk questionnaire, play a short game about risk, say what you'd like to hold, and you get a portfolio that fits you, with every number explained and checked by a second, independent piece of code. An AI assistant explains the result and can try "what if" questions, but it never decides anything. If you confirm a plan, a fingerprint of it goes on a public test blockchain so you can prove later that nobody changed it.

This repository is the engine and the API (Python, FastAPI). It is a snapshot of the version we submitted on 20 October 2026 (tag `submission-20oct`); our day-to-day history stays in a private repository.

| | |
|---|---|
| Website | https://www.egoquant.app |
| API (interactive docs) | https://web-production-59960.up.railway.app/docs |
| Backend repository (this one) | https://github.com/EgoQuantAI/egoquant-backend |
| Frontend repository | https://github.com/EgoQuantAI/egoquant-frontend |
| Technical documentation | [`submission/1_Technical_Documentation/00_EgoQuant_Technical_Documentation.pdf`](submission/1_Technical_Documentation/00_EgoQuant_Technical_Documentation.pdf) (one PDF) or [`docs/judges/`](docs/judges/README.md) |
| Deployment instructions | [`DEPLOYMENT.md`](DEPLOYMENT.md) |

## Try it in two minutes, without installing anything

Open the website, choose **Verify a report** in the sidebar, press **Use the demo report**, then **Try a tampered copy**. The first checks a real sealed report against the Sepolia test chain; the second shows what happens when one number was changed. For the whole path (questionnaire, Risk Game, portfolio, advisor) see [`docs/judges/05_USER_GUIDE_AND_DEMO.md`](docs/judges/05_USER_GUIDE_AND_DEMO.md).

## What is where

| Path | What it is |
|---|---|
| `main.py`, `egoquant_routes.py`, `chat.py` | The FastAPI app, its routes and the advisor chat |
| `egoquant/risk_profile/` | Questionnaire, scoring, the C1 to C5 category and its limits |
| `egoquant/agents/market_intel/` | Agent C: product catalogue (R1 to R5), allowed universe, prices |
| `egoquant/agents/quant/`, `egoquant/regime/` | Agent A: candidate portfolios and the goal simulation |
| `egoquant/agents/behavioral/` | Agent B: behaviour from a real trade history; can only tighten limits |
| `egoquant/risk_engine.py` | The independent Risk Engine that has the final say |
| `egoquant/advisor/`, `egoquant/briefing/` | Advisor safeguards; the read-only news briefing |
| `egoquant/accounts/`, `supabase/` | Accounts (Supabase, Row-Level Security) and the database migrations |
| `egoquant/anchoring/` | The on-chain seal and its verifier |
| `tests/`, `eval/` | Automatic tests; evaluation scripts, red-teams and the deploy check |

## Run it on your machine

You need Python 3.11 or newer (we run 3.14). Nothing else: the repository carries a saved snapshot of real market prices, so no API key is needed to build portfolios.

```bash
git clone https://github.com/EgoQuantAI/egoquant-backend.git
cd egoquant-backend
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
export EGOQUANT_PRICE_MODE=snapshot    # Windows: set EGOQUANT_PRICE_MODE=snapshot
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs to call every endpoint from the browser: `GET /questionnaire` reads the questions, `POST /profile` scores answers, `POST /portfolio` builds a portfolio. To use the website against it, see the frontend's README.

These parts stay off until you give them a key, and everything else works without them:

| Feature | What you need | Without it |
|---|---|---|
| Advisor chat | `OPENAI_API_KEY` | The chat says it is unavailable; reports, limits and trade checks still work |
| Live prices | `TIINGO_API_KEY` and `EGOQUANT_PRICE_MODE=live` | The saved snapshot of real prices (to 16 October 2026) is used and labelled |
| Accounts and saved reports | a Supabase project (`SUPABASE_URL`, `SUPABASE_ANON_KEY`) | Everything works as a visitor; nothing is saved |
| Sealing on the test chain | `EGOQ_SEAL_ENABLED=1`, `EGOQ_SEAL_PRIVATE_KEY`, `EGOQ_SEAL_RPC_URLS` | Plans can be confirmed; no fingerprint is published |

Every setting, with a sentence each, is in [`docs/DEPLOY_ENV.md`](docs/DEPLOY_ENV.md).

## Test cases

```bash
pytest -q -m "not live"
```

3,554 tests, with no network and no AI calls; 7 to 35 minutes depending on the machine. `-m "not live"` leaves out the 21 tests that need live market data. On Windows the numerical libraries sometimes crash a long test process; then run one folder at a time with `python eval/run_tests_per_file.py . results.txt --dir tests/system` (explained in [`docs/judges/04_TESTING_AND_EVIDENCE.md`](docs/judges/04_TESTING_AND_EVIDENCE.md), section 9).

| Folder under `tests/` | Tests | What it covers |
|---|---:|---|
| `agent_b` | 680 | Behaviour from a trade history, the IBKR file parser including hostile files, the crash test |
| `anchoring` | 464 | The on-chain seal: record, fingerprint, sealed report, every committed proof, tampering attempts |
| `briefing` | 414 | News items, tone labels, the advice filter, background jobs |
| top level | 407 | Routes, request-size limits, chat, price-feed fallback drills, plain wording, no echo of user input |
| `system` | 360 | The whole chain: pipeline, Risk Engine, report, goal simulation, what-if, trade check, Risk Game blend |
| `agent_a` | 287 | The optimiser, the backtest and the optional second method |
| `agent_c` | 274 | Universe choice, price sources, snapshots, caches, fallbacks |
| `accounts` | 265 | User A can never reach user B's data, saved reports, hostile inputs, the chat's trade tool |
| `risk_profile` | 134 | Questionnaire, scoring, the C1 to C5 table |
| `advisor` | 105 | Advice guard, trade explanations, behaviour when the AI is down |
| `regime` | 90 | The market-phase sampler and its fall-back |
| `preferences` | 60 | The investment-preference form |

The counts are from `pytest --collect-only` on the final candidate, before the last changes brought the total to 3,554; [`docs/judges/04_TESTING_AND_EVIDENCE.md`](docs/judges/04_TESTING_AND_EVIDENCE.md) has the last complete runs.

Beyond the unit tests:

- `python eval/deploy_check.py https://your-backend`: read-only check of a deployed copy (status, security headers, CORS, demo builds, trade checks). The live copy passes 71 of 71.
- `supabase/tests/rls_isolation.sql` and `rls_onchain.sql`: Row-Level Security proofs to run on a real Supabase project; both pass on ours.
- `eval/chat_redteam.py` and `eval/trade_explain_redteam.py`: live red-teams of the AI (they call OpenAI and cost a few cents); results are kept in `eval/results/`.
- `python -m egoquant.anchoring verify <sealed report or seal file>`: checks a seal against the chain without our website.

## Deploy it

[`DEPLOYMENT.md`](DEPLOYMENT.md). Short version: the backend is one Python process (we use Railway), the website is a Next.js app (we use Vercel), accounts are a Supabase project.

## What it is not

It places no orders and moves no money. The category limits are our own placeholders, not a bank's policy. Every chance it shows is a simulation on past prices, not a forecast. What we know is weak is listed in [`docs/judges/06_LIMITATIONS_AND_ROADMAP.md`](docs/judges/06_LIMITATIONS_AND_ROADMAP.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

## The team

Savas, Umut Bideci, Aslan and Zerya, working with several Claude Code sessions (Anthropic's Claude) that wrote and reviewed much of the code and the documents under the same rules as everyone: full test suite before every merge, a secret scan on every change. The running product does not use Claude; the advisor uses OpenAI models.

The long technical README of the development repository is [README_DETAILED.md](README_DETAILED.md).
