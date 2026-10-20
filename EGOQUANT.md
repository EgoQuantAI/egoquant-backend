# EgoQuant backend

A deterministic wealth-advisory engine. It turns a formal risk questionnaire, the investor's
interests and real market data into a portfolio proposal. An independent Risk Engine re-checks the
proposal, and the result is written up as a client report. No LLM makes any decision; an Advisor
chatbot may only rephrase the report. (One narrow exception: Agent D's fund-aware news-tone label, which
feeds only the briefing shown beside the report.)

```
Risk questionnaire ──> C1–C5, product R-level, loss capacity, knowledge restrictions
        │
        ├──> Agent C (market intel): approved universe + REAL prices (Tiingo ETFs, Binance crypto)
        │
        └──> Agent A (quant): candidate menu ──PortfolioMenu──> Agent B (behavioral twin)
                     ▲                                               │
                     └───────── numeric limits (tighten only) ───────┘
                     │
                     ▼
             final proposal ──> Risk Engine (independent re-check, final say) ──> client report
```

Agent D (market briefing, `egoquant/briefing/`) reads the approved portfolio and shows news tone and summaries
beside the report; it never changes a weight, limit or verdict. All agents are connected in `run_pipeline`.

## Accounts, IBKR data, saved reports, trade check

| Piece | Where | Rule |
|---|---|---|
| Sign-up / login | Supabase Auth (email confirmation), cookies set by the Next.js routes | Visitors can still use everything without an account. |
| Row-Level Security | `supabase/migrations/001_accounts.sql`, proof in `supabase/tests/rls_isolation.sql` | Every query runs as the user; no service-role key. |
| IBKR trading history | `/portfolio` (used once) or `/account/trades/import` (saved); Flex report upload or Flex token | Read-only. Token and file are never stored. Agent B reads the history and can only tighten. |
| Saved reports | `POST /account/portfolio`, `GET/DELETE /account/reports[/{id}]` | Built from the saved history; own rows only. |
| Trade check | `POST /account/trade-check`, `egoquant/trade_check.py` | One planned trade against the investor's limits and the Risk Engine; verdict `advisable`, `allowed_with_warnings` or `not_advisable`. Deterministic; the chatbot only phrases it. EgoQuant never trades. |
| Trade check without an account | `POST /trade-check` | Same check; the holdings come from the IBKR report or token sent with that one request. Nothing is stored and "I made this trade" needs an account. Both routes also return `trade_context` (the check sealed for the advisor chat, which explains it in plain words and may not change a number or the verdict). |
| "I made this trade" | `POST /account/trade-checks/{id}/decision` | Records what the user reports (source `user_recorded`) and logs it. |
| Operation log | `GET /account/log` | Actions, times and labels only. |

Security details, threats and open gaps: [docs/SECURITY.md](docs/SECURITY.md).

## Layout

| Path | What it is |
|---|---|
| `egoquant/models.py` | Shared contracts: `InvestorContext`, `Constraint`, `PortfolioProposal`, C1–C5 class policies |
| `egoquant/scenarios.py` | Hypothetical stress scenarios, shared by Agent A, Agent B, the Risk Engine and the report |
| `egoquant/risk_profile/` | Questionnaire scoring → `RiskProfile` (C1–C5) → limits ([README](egoquant/risk_profile/README.md)) |
| `egoquant/agents/market_intel/` | Agent C: instrument catalog, trading platforms, price sources, disk cache |
| `egoquant/agents/quant/` | Agent A: `agent.py` (QuantAgent), `core.py` (math, optimizer), `backtest.py` |
| `egoquant/agents/behavioral/` | Agent B: trading logs + risk game → behavioral limits ([README](egoquant/agents/behavioral/README.md)) |
| `egoquant/risk_engine.py` | Independent re-check: weights, R-levels, equity cap, class caps, stress losses |
| `egoquant/report.py` | Client report: structured `ClientReport` + one self-contained HTML page |
| `egoquant/pipeline.py` | `run_pipeline`: the whole chain for one investor |
| `egoquant/briefing/` | Agent D: read-only market briefing (news tone and summaries) |
| `egoquant/accounts/` | Account endpoints, Supabase client, IBKR input |
| `egoquant/trade_check.py` | Deterministic check of one planned trade |
| `egoquant/regime/` | Optional regime-aware sampler for the goal Monte Carlo (HMM fitted offline, numpy at runtime); ON by default, `EGOQUANT_REGIME_MC=0` switches it off, falls back to the block bootstrap on any error (Zerya) |
| `egoquant/advisor/` | Advisor chat: sealed engine facts, compliance guard |
| `egoquant/demo/` | Trader personas, SYNTHETIC investors and price paths, offline stand-in agents |
| `egoquant/cli.py` | `python -m egoquant ...` |
| `egoquant/settings.py` | Tiingo key (env `TIINGO_API_KEY` only) and the price cache directory |
| `tests/` | One folder per component, plus `tests/live` on real market data |

## What each risk class may hold

| Class | Products up to | Stocks | Gold & commodities | Crypto | Cash (Agent A cap) |
|---|---|---|---|---|---|
| C1 | R1 (T-bills, short Treasuries) | 0% | 0% | 0% | 100% |
| C2 | R2 (+ investment-grade bonds) | 0% | 0% | 0% | 100% |
| C3 | R3 (+ broad equity, gold, long Treasuries) | 40% | 10% | 0% | 30% |
| C4 | R4 (+ sector/EM/small caps, REITs, silver, commodities) | 70% | 15% | 0% | 20% |
| C5 | R5 (+ crypto, only with full product knowledge) | 90% | 20% | 5% | 15% |

All numbers are PROVISIONAL team proposals, not a bank's approved policy. The source of truth is
`egoquant/risk_profile/config.yaml`. An investor's own cash need always overrides the cash cap.

## Run it

```bash
pip install -e ".[dev]"
python -m egoquant personas                    # the demo trader profiles
python -m egoquant advise crypto_trader        # full chain on REAL prices -> reports/crypto_trader_<date>.html
python -m egoquant advise balanced_saver --synthetic        # offline, invented prices
python -m egoquant fetch all --out prices.csv  # save real prices, then: advise <persona> --csv prices.csv
python -m egoquant behavior                    # Agent B on its synthetic investors
python -m egoquant backtest --seeds 5          # Agent A vs simple baselines (synthetic paths)
```

Real prices: Tiingo (US-listed ETFs; set `TIINGO_API_KEY` in `.env` or the environment, see
`.env.example`) and Binance's public market-data API (crypto spot, no key). Fetched prices are cached
in `~/.cache/egoquant` (or `EGOQUANT_CACHE_DIR`), so repeated runs stay inside Tiingo's free-tier limits.
If a live feed fails, Agent C uses its committed snapshot of real prices,
`market_intel/data/prices_snapshot_yahoo.json.gz` (292 catalog tickers, built offline from Yahoo Finance and
Binance; rebuild it with
`python -m egoquant.agents.market_intel.snapshot --as-of <last close> --source yahoo --include-stocks`,
which needs `pip install yfinance` on that machine only). The Tiingo snapshot `prices_snapshot.json.gz` stays
for the regime model.

## Tests

```bash
pytest -q                  # all tests; tests/live builds every persona's portfolio from real prices
pytest -q -m "not live"    # offline only
```

| Folder | Covers |
|---|---|
| `tests/risk_profile` | scoring, gate, knowledge, adapter, C-level table in sync with Agent A |
| `tests/agent_a` | math core, the agent (classes, caps, regimes, Agent B limits), backtest harness |
| `tests/agent_b` | behavioral detection and the limits it sends Agent A |
| `tests/agent_c` | universe selection, R-level rule, platforms, sources, cache, snapshots |
| `tests/system` | pipeline for C1–C5, Risk Engine, client report |
| `tests/live` | the six personas on real data: approved, fresh prices, every C-level rule respected |
