# EgoQuant backend

EgoQuant is a wealth-advisory agent built for the 2026 Shenzhen FinTechathon (WeBank International Track,
Topic B: Wealth Advisory Agent). A formal risk questionnaire, the investor's interests and real market data go
in. Out comes a portfolio proposal that an independent Risk Engine re-checks, a client report, a news briefing
about the investor's holdings, and an Advisor chat that explains all of it.

**Scoring and portfolio decisions are deterministic code. LLMs only explain**, with one documented exception:
the briefing's news-tone label (see Agent D below). That label never sets a score, a limit, a weight or an
approval.

- Live demo: https://fintechaton-frontend.vercel.app
- API: https://web-production-ee5cc.up.railway.app (interactive docs at `/docs`)
- Backend repository: https://github.com/EgoQuantAI/egoquant-backend
- Frontend repository: https://github.com/EgoQuantAI/egoquant-frontend

**For judges: start with [`docs/judges/README.md`](docs/judges/README.md).** Nine short documents, in the order a judge needs them: the project summary, the security and compliance report, the methods, the testing evidence, the user guide with screenshots, the limitations, the questions we expect, the frontend and API reference, and the on-chain seal. Every claim in them names a file, a test or a measured number.

## Architecture

```
Risk questionnaire ──> C1–C5, product R-level, loss capacity, knowledge restrictions
        │
        ├──> Agent C (market intel): approved universe + real prices (Tiingo ETFs, Binance crypto)
        │
        └──> Agent A (quant): candidate menu ──PortfolioMenu──> Agent B (behavioral twin)
                     ▲                                               │
                     └───────── numeric limits (tighten only) ───────┘
                     │
                     ▼
             final proposal ──> Risk Engine (independent re-check, final say) ──> client report
                                                                                     │
             Agent D (market briefing, read-only) ── news tone + "What changed" ─────┤
             Advisor chat (LLM) ── explains the sealed report facts, never decides ──┘
```

| Piece | Code | What it does |
|---|---|---|
| Risk questionnaire | `egoquant/risk_profile/` | Grable & Lytton tolerance, capacity, knowledge gate → C1–C5 and numeric limits ([README](egoquant/risk_profile/README.md)) |
| Agent A (quant) | `egoquant/agents/quant/` | Optimizer, candidate menu, goal simulation, backtest |
| Agent B (behavioral twin) | `egoquant/agents/behavioral/` | Trading history (incl. IBKR Flex reports) and the risk game; can only tighten limits ([README](egoquant/agents/behavioral/README.md)) |
| Agent C (market intel) | `egoquant/agents/market_intel/` | Instrument catalog with R-levels, approved universe, price sources, disk cache |
| Agent D (market briefing) | `egoquant/briefing/` | News per holding and interest area, fund-aware tone, source tiers, "What changed" ([README](egoquant/briefing/README.md)) |
| Risk Engine | `egoquant/risk_engine.py` | Independent re-check of weights, R-levels, class caps, stress losses; has the final say |
| Report | `egoquant/report.py` | `ClientReport` (JSON) and a self-contained HTML page |
| Pipeline | `egoquant/pipeline.py` | `run_pipeline`: the whole chain for one investor |
| Advisor chat | `chat.py`, `egoquant/advisor/` | OpenAI Responses API; answers only from HMAC-sealed report facts; compliance guard |
| Accounts | `egoquant/accounts/`, `supabase/` | Optional Supabase sign-in, stored IBKR history and saved reports (row-level security) |
| Web API | `main.py`, `egoquant_routes.py` | FastAPI app used by the frontend |

More detail: [EGOQUANT.md](EGOQUANT.md) (engine overview and risk-class table) and [CLAUDE.md](CLAUDE.md) (team
rules, principles and open decisions).

## Run locally

Python 3.14 (see `.python-version`).

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
cp -n .env.example .env          # then fill in the keys you have; never commit .env
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs. `GET /health` needs no key. The engine also runs from the command line:

```bash
python -m egoquant advise balanced_saver --synthetic                    # offline, invented prices
python -m egoquant advise growth_investor --synthetic --briefing --period month   # + Agent D briefing
python -m egoquant advise crypto_trader                                 # real prices (needs TIINGO_API_KEY)
python -m egoquant.briefing                                             # offline briefing demo (sample news)
```

## Tests

```bash
pytest -q -m "not live"    # everything offline (no network, no OpenAI calls)
pytest -q -m live          # demo profiles built from REAL market data (needs network and TIINGO_API_KEY)
```

## Deployment (Railway backend, Vercel frontend)

Both repositories deploy from the `oct19` branch (the working branch since 19 Oct; `october18` is kept equal to it).
Work happens on feature branches that are merged into `oct19`. Railway runs the `Procfile` (`uvicorn main:app --host 0.0.0.0 --port $PORT`). Set these variables on
Railway (names only; values never go into the repository):

| Variable | Needed for | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Advisor chat, briefing tone and summaries | Set an OpenAI project spending limit |
| `OPENAI_MODEL` | Advisor chat model | Optional |
| `EGOQUANT_CONTEXT_SECRET` | Sealing the chat context | Long random string |
| `TIINGO_API_KEY` | Real ETF prices (traders) | Free key at tiingo.com |
| `TIINGO_WARM_API_KEY` | Background price-cache builder | Optional second key |
| `EGOQUANT_BRIEFING` | News briefing mode | `live` (default), `fixture` (DEMO sample news), `off` |
| `EGOQUANT_BRIEFING_LLM` | Fund-aware news tone on the web | `1` to enable; capped per request and per hour |
| `EGOQUANT_CACHE_DIR` | Price cache, tone cache, news archive | Point at a mounted Railway volume (e.g. `/data/egoquant`) |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Accounts (optional) | Without them `/account` answers 503 and everything else works |
| `CORS_ORIGINS` | Browser origins | Default: the Vercel site |
| `EGOQUANT_PRICE_MODE` | Where prices come from | `live` (default: Tiingo/Binance first, the committed snapshot as the fallback) or `snapshot` (the committed snapshot of real prices as of 16 Oct 2026, ZERO live requests: fastest and the right choice for the demo and for portfolio method v2) |
| `EGOQUANT_LIVE_BUDGET_SECONDS` | Slow live feed guard | Default 15: after this much waiting for live prices in one build the whole build switches to the snapshot |
| `EGOQUANT_SLOW_REQUEST_SECONDS` | Slow live feed guard | Default 8: two live requests slower than this also switch the build to the snapshot |
| `EGOQUANT_PORTFOLIO_METHOD` | Portfolio construction | `v1` (default) or `v2` (docs/PORTFOLIO_V2.md); anything else is `v1` |
| `EGOQUANT_RISK_GAME_WEIGHT` | Risk category | Share of the Risk Game in the tolerance part of the category (default `0.3333` = one third; `0` ignores the game everywhere; clamped to 0..0.5). docs/RISK_GAME_BLEND.md |
| `EGOQUANT_RISK_GAME_MAY_RAISE` | Risk category | `off` lets the game only LOWER the tolerance part; anything else (default) lets it also raise it, never above the capacity cap |

**The Railway disk is temporary.** Without a volume, every redeploy loses the price cache, the news-tone cache
and the news archive (they rebuild, at a few cents of OpenAI and some Tiingo requests). For accounts, run
`supabase/migrations/001_accounts.sql` in the Supabase project once.

## Evaluation

News tone, judged against 120 headlines labeled by the team for holders of the matched fund
([eval/results.md](eval/results.md)):

| Method | Accuracy |
|---|---|
| Fund-aware LLM (gpt-4.1-mini, the production prompt) | 85% |
| FinBERT | 53% |
| Loughran-McDonald word counting | 38% |
| Always "neutral" | 38% |

Two human labelers agreed with each other on 75% of headlines before adjudication. With 120 headlines the 95%
interval is about ±6 points near 85%. The Advisor chat has a red-team script (`python eval/chat_redteam.py`).

## Known limitations

- **PROVISIONAL numbers.** Every cutoff, cap and weight is a team proposal in config files, not a bank's
  approved policy.
- **Shared rate limits.** Tiingo's free tier (50 requests an hour) is shared by all visitors. A busy demo can
  fall back to Agent C's committed price snapshot (real prices from Yahoo Finance and Binance, frozen on its
  date; used for up to 30 days after it).
- **Debug endpoint, off by default.** `/debug/portfolio` runs demo personas on synthetic prices. It answers 404
  unless `EGOQUANT_DEBUG_PAGES=on` (docs/DEPLOY_ENV.md); when on, it is unauthenticated with its own small rate limit.
- **Temporary disk.** Without a Railway volume, caches and the news archive are lost on redeploy. The monthly
  news view then shows "not enough coverage last month" until history builds up again.
- **No real money.** Nothing is executed, and this is not regulated advice.
- **News tone describes headlines.** It is not a forecast, and it is wrong about 15% of the time on our
  labeled set.
