# EgoQuant backend: project brief

EgoQuant is a wealth-advisory agent built by a 5-person team for the 2026 Shenzhen FinTechathon
(WeBank International Track, Topic B: Wealth Advisory Agent). Submission deadline: **Oct 20, 2026,
23:59 Beijing time**. Scoring: task completion 40%, security & compliance 30%, innovation & interaction 30%.

## Architecture

```
Risk questionnaire ──> C1–C5 + numeric limits (Constraint, source="suitability")
        │
        ▼
Agent A  (deterministic optimizer, never sees the score) ──PortfolioMenu──> Agent B (behavioral twin)
        ▲                                                              │
        └──────────── list[Constraint] (source="behavioral", tighten only) ┘
        │
        ▼
Agent A re-optimizes ──PortfolioProposal──> Risk Engine (independent re-check) ──> LLM advisor explains
Agent C (market intel) supplies the approved universe and prices to Agent A.
Agent D (market briefing, read-only) reads the approved portfolio, Agent C's selection and the investor's
interests, and shows news tone and summaries ALONGSIDE the report. It never feeds back into A, B or the
Risk Engine; its only action is offering a risk-profile re-check.
```

| Piece | Code | Owner |
|---|---|---|
| Risk questionnaire | `egoquant/risk_profile/` ([README](egoquant/risk_profile/README.md)) | Bartu |
| Investment preferences | `egoquant/preferences/`: form shown after the C-level (allowed options only) → Agent C | |
| Agent A: quant optimizer | `egoquant/agents/quant/` (`agent.py`, `core.py`, `backtest.py`) | Damla |
| Agent B: behavioral twin | `egoquant/agents/behavioral/` ([README](egoquant/agents/behavioral/README.md)) | Ryukazaki |
| Agent C: market intel | `egoquant/agents/market_intel/` (directory of every US stock/ETF by cluster, curated catalog + exposure groups, platforms, sources + disk cache, `fetch_universe` cluster walk) | Damla |
| Shared contracts | `egoquant/models.py` (`Constraint`, `InvestorContext`, `PortfolioProposal`, …), `egoquant/scenarios.py` (stress scenarios) | team |
| Risk Engine | `egoquant/risk_engine.py`: independent re-check of R-level, equity cap, class caps, stress loss | Bartu (pending team confirmation) |
| Reports | `egoquant/report.py` | |
| End-to-end pipeline | `egoquant/pipeline.py` (`run_pipeline`): questionnaire → C → A (+ B) → Risk Engine → report (+ optional Agent D briefing and "What changed", after the verdict) | |
| Demos and synthetic data | `egoquant/demo/` (trader personas, synthetic investors and prices, stubs), `egoquant/cli.py` | |
| Agent D: Market Briefing (read-only) | `egoquant/briefing/` ([README](egoquant/briefing/README.md)): news tone and summaries shown alongside the portfolio | Bartu |
| Advisor chat | `egoquant/advisor/` (engine facts, HMAC-sealed `chat_context`, compliance guard + `config.yaml`), HTTP in `chat.py` (`/chat`, `/chat/stream`), live eval `eval/chat_redteam.py` | Umut |
| Settings | `egoquant/settings.py`: Tiingo key from env `TIINGO_API_KEY` only (`.env` is read), price cache directory | |

## Core principles (do not break)

- **Scoring is deterministic code, never an LLM.** Same inputs → same outputs. LLMs only explain.
  *One precise exception:* Agent D's news tone. For each (headline, fund) pair, gpt-4.1-mini labels the
  headline positive / negative / neutral **for holders of that one fund**. It uses the fixed, evaluated prompt
  `briefing/prompts/fund_aware_tone.txt` (SHA-256-pinned; 85% agreement with team labels vs 53% for FinBERT),
  temperature 0, a cache shared across investors, and FinBERT as the automatic fallback. Only the headline and
  the fund are sent. The label feeds only the briefing's tone aggregates and the alert that offers a
  risk-profile re-check. It never sets a questionnaire score, a C-level, a limit, a weight or the Risk Engine's
  verdict.
- **Agent A never sees the questionnaire score.** It gets the C-level (`InvestorContext.risk_class`,
  turned into class limits) and numeric suitability limits, nothing else from the questionnaire.
  Don't expose the C-level anywhere it isn't needed (decided, see open decision 10).
- **The Risk Game is one third of the tolerance part of the category** (Savas, 20 Oct; `docs/RISK_GAME_BLEND.md`). Everyone is offered it and
  encouraged to play. Category = the LOWER of the capacity cap and the blended tolerance (two thirds questionnaire level, one third game
  level, rounded); the no-loss gate still forces C1; no game = the questionnaire alone. The game rides inside `answers.risk_game`, so every
  endpoint that takes answers scores the same category (`_score(answers, game)`). Agent B does not count the game's two scores a second
  time (`BehaviorConfig.game_in_profile`).
- **Behavior only tightens.** Agent B can lower limits, never raise them (trade log and crash test; the game is in the category above).
- **The Risk Engine has the final say.** Agent A only proposes.
- Every stress result is labeled hypothetical; every historical number is labeled in-sample. Nothing is a forecast.

## Conventions

- **Shared files change only by team agreement.** `egoquant/models.py` is shared. Module-specific
  models go in the module (`egoquant/agents/behavioral/models.py`, `egoquant/risk_profile/models.py`).
  Before editing another owner's file, show the plan and get approval.
- **All numbers live in config, marked PROVISIONAL.** No cutoffs or caps are hard-coded in logic.
  The C-level table's source of truth is `egoquant/risk_profile/config.yaml` (R-level, equity,
  commodity and crypto caps, crash loss). Agent A's `DEFAULT_RISK_CLASS_POLICIES` copies it, and a
  test keeps the two in sync. Product R-levels live only in Agent C's catalog.
- **Validated instruments are quoted verbatim.** The Grable & Lytton items and the Lusardi-Mitchell Big
  Three must not be paraphrased. Sources are in the risk_profile README.
- Pure functions for scoring and math. I/O sits in thin loaders (`risk_profile/loader.py`, Agent C's `sources.py`).
- Synthetic data lives in `egoquant/demo/` only and is always labeled "synthetic".
- Reuse the shared `Constraint` class. Never define a second one.
- Work on feature branches and merge them into `oct19`, the working and deploy branch since 19 Oct (Railway backend,
  Vercel frontend; `october18` is kept equal to it). Run `pytest -q -m "not live"` (and the frontend's lint and build)
  before merging; never force-push. `main` mirrors `oct19` for judges.

## Agent D: Market Briefing (read-only) rules (`egoquant/briefing/`)

- Shown **alongside** the portfolio. It never changes Agent A's inputs or weights, Agent B's limits or the
  Risk Engine's decision. Its only action is an alert that **offers** a risk-profile re-check
  (`offer_risk_profile_recheck`); nothing happens unless the user confirms.
- No buy/sell/hold advice and no forecasts. The advice filter (`briefing/config.yaml` `forbidden_patterns`)
  runs on every LLM summary, every template and every rendered line.
- Every number (exposure, counts, dates, aggregates, changes) is computed in code. The tone per (headline,
  fund) is the **fund-aware LLM label** (+1 / 0 / −1; `tone.method: llm_fund_aware`, see the core-principle
  exception). **FinBERT** (local, free; gated P(pos) − P(neg)) is the automatic fallback when there is no key,
  an API error, `llm.enabled: false`, the per-run cap, or an unscored answer. Each item records its
  `tone_method`, and the report shows counts per method. A pair with neither label is "tone not yet assessed"
  and is left out of scores and alerts. OpenAI also writes the neutral summary sentences; their `llm_tone` is
  stored for evaluation only. No paid news or sentiment APIs.
- The prompt `briefing/prompts/fund_aware_tone.txt` is never edited (a test pins its SHA-256); a new prompt
  needs a new evaluation.
- OpenAI spend controls: summaries are cached by item id + model + prompt version and capped by
  `max_llm_calls_per_run`; tone labels are cached by (headline id, ticker, prompt hash, model) and capped by
  `tone.llm_max_calls_per_run`; the web path calls only with `EGOQUANT_BRIEFING_LLM=1`, at most 8 per request,
  inside the 5-second budget; the pairs left over (up to 60 per request, 400 calls an hour) are labeled after the
  response by one background worker into the same cache (`EGOQUANT_BRIEFING_TONE_BACKGROUND=off` disables it;
  tests do). The server has no FinBERT, so without `EGOQUANT_BRIEFING_LLM=1` every headline is "tone not yet
  assessed". Everything can be switched off with `llm.enabled`. Tests never call OpenAI.
- No investor data is ever sent to the LLM (`summarize.LLMRequest` has only source, title and text; a tone
  request has only the headline and one fund). Article text is untrusted data.
- In the pipeline the briefing is optional (`run_pipeline(briefing=..., briefing_period="week"|"month")`,
  CLI `advise … --briefing [--period week|month]`). It is built after the Risk Engine verdict and gets no
  weights when the proposal is rejected. The report's "What changed" section (Agent C drawdown alerts +
  Agent D tone alerts, built from the numbers, advice-filtered) has `offer_risk_profile_recheck` as its only
  action.
- R-levels, tickers and interest areas come only from Agent C's catalog.
- Sample (fixture) items are invented, under invented source names, and briefings built from them carry
  the banner "DEMO: sample news items, not real news."
- **Sources and dates.** The publisher is the real one: Yahoo by link domain, Nasdaq by `<author>`, Google by
  `<source>`. Tiers are set in config `sources` (PROVISIONAL, a reason per source): established 1.0, commentary
  0.7, unrated 0.4. Low-reliability (tier3) items are listed but never used for tone or alerts and never sent to
  the LLM. Dates come from the feed only (`date_status` published / updated_only / unknown). Unknown dates are
  excluded from tone windows; dates more than 15 minutes in the future are rejected. Article pages are never
  fetched.
- **Every headline is rated; no time cap decides coverage.**
  - A background news worker (`briefing/worker.py`, started in `main.py`; off with `EGOQUANT_NEWS_WORKER=off`)
    fetches feeds for up to 120 funds every 15 minutes and archives the headlines. It rates new (headline, fund)
    pairs within `labels_per_run` and `labels_per_day` (3000).
  - `/portfolio` returns a sealed `briefing_context` (approved weights only). `POST /briefing/start` and
    `GET /briefing/{job}` build the full briefing outside the request, rating every relevant pair (holdings
    first, 20 at a time, with retries). The ceilings are only a safety net; leftovers go to the background and
    show as "rating in progress".
  - Headline-only items get no LLM summary.
- **News tab structure.** Every holding gets a card. Only the areas the investor chose get cards. Each headline
  is shown once. Coverage is a tone, "not enough coverage (N; needs 3)", or "not yet assessed" (never the last
  when anything is assessed). `/portfolio` returns the week (`market_briefing`, `what_changed`) and the month
  (`market_briefing_month`, `what_changed_month`) from ONE fetch. The toggle is client-side.

## Advisor chat rules (`egoquant/advisor/`, `chat.py`)

- The LLM only explains engine facts. Profile facts are re-scored from the answers on every message;
  portfolio facts are trusted only inside the HMAC-sealed `chat_context` from `/portfolio`.
- No questionnaire answers, points or investor id in the facts; the score appears only inside the same
  plain-language explanation the Profile page shows. Briefing text inside the facts is untrusted data.
- Every reply passes the guard sentence by sentence before the user sees it (also while streaming).
- Tests mock OpenAI. `eval/chat_redteam.py` is the live check; run it after any prompt change and bump
  `PROMPT_VERSION` in `chat.py`.

## Running

```bash
pip install -e ".[dev]"
pytest -q                     # everything; tests/live uses real market data (skips without network)
pytest -q -m "not live"       # offline only
python -m egoquant personas   # demo trader profiles
python -m egoquant advise crypto_trader            # full chain on real data -> reports/*.html
python -m egoquant advise balanced_saver --synthetic
python -m egoquant behavior | backtest | fetch all --out prices.csv
python -m egoquant.briefing                       # market briefing demo, offline (sample news)
```

Tests are grouped by component: `tests/risk_profile`, `tests/agent_a`, `tests/agent_b`, `tests/agent_c`,
`tests/system` (pipeline, Risk Engine, report), `tests/preferences` and `tests/live`.

## Open decisions

These are recommendations from the risk-questionnaire build report (Oct 17). **None is decided yet**
unless marked otherwise.

1. **Where R-levels are enforced.** Recommendation: both layers. *Status:* implemented in the
   PR #2 (merged Oct 17 without review). Layer 1: Agent C drops instruments above `max_r_level`,
   and the adapter backs it up with `excluded_assets`. Layer 2: the Risk Engine re-checks every holding.
2. **R-level of each asset.** Agent C's catalog (`Instrument.r_level`) is the single source. There is no
   second list. Proposed rule: cash/money-market R1; government and investment-grade bond ETFs R2; broad
   equity index ETFs R3; sector/emerging-market/single-country equity ETFs R4; crypto and leveraged
   products R5. The catalog differs for SHY (R1) and TLT (R3); kept as-is for now. Gold (IAU) is R3,
   silver (SLV) and broad commodities (PDBC) R4; the rule is written in `catalog.py` and a test checks
   every rating against it. Owner: Agent C's owner.
3. **One equity-cap table.** Recommendation: `risk_profile/config.yaml` (0/0/40/70/90%) is the single
   source. *Status:* Agent A's table was aligned to it on `main` (commit 6017058). Still needs team sign-off.
4. **C2 stock exposure.** Recommendation: keep 0% for the MVP. Add an R2 bond-heavy mixed fund only if time allows.
5. **Consistency-check threshold (0.5).** Recommendation: keep it, then try 5–6 invented personas and adjust.
6. **3 Big Three correct but no experience.** Recommendation: keep the built default (warning plus acknowledgement).
7. **"Refuse to answer" on K1–K3.** Recommendation: hide it in the UI and keep "Do not know".
8. **Illustrative crash losses per C-level.** Recommendation: replace with Agent A stress-test results
   when available. Until then, keep the "illustrative" label.
9. **System Context Document.** It still describes a conversational questionnaire. Recommendation: update it
   to "formal questionnaire" with this module's design.
10. **Score-blindness.** *Decided:* Agent A and Agent B may see the C-level (`InvestorContext.risk_class`,
    `PortfolioMenu.risk_class`); the score itself never leaves the risk profile. Don't expose the
    C-level where it isn't needed.
11. **Gate stress-loss limit.** No-loss investors get `max_stress_loss = 0`, so only assets that do not fall
    in the capacity scenario (cash) can be held. `InvestorContext.max_acceptable_loss` accepts 0
    (PR #1, merged Oct 17 without review; it touched shared files `models.py`, `quant.py`, `report.py`).
12. **Leveraged/inverse products** are not in Agent C's catalog, so "blocked complex" covers crypto only.
13. **C1 per-asset cap.** *Decided (Oct 17):* C1 investors may hold up to 100% in one fund
    (`risk_profile/config.yaml` `adapter.r1_max_asset_weight: 1.0`). Agent A's default 40% cap made C1
    infeasible, because only two R1 funds exist (BIL, SHY). The cap applies per fund, but each R1 fund is
    internally diversified across many government bills or notes. Gate investors can only hold BIL anyway.
    So a one-fund C1 portfolio (e.g. 100% SHY or 100% BIL) is accepted. If it looks odd to judges,
    add a third R1 fund to Agent C's catalog rather than lowering the cap.
14. **Gold, commodities and crypto in Agent A** (`chore/reorganize-repo`). New asset classes "commodity"
    and "crypto" with per-class caps in the C-level table: commodities 0/0/10/15/20%, crypto 0/0/0/0/5%.
    Agent A also caps cash at 30/20/15% for C3/C4/C5 (never below the investor's own cash need), because
    a minimum-variance optimizer otherwise parks a growth portfolio's non-equity part in T-bills. Stress
    shocks for the new classes are in `egoquant/scenarios.py`. All PROVISIONAL; needs team sign-off.
15. **Tiingo key.** *Fixed:* it is read only from `TIINGO_API_KEY` (environment or the gitignored `.env`);
    the built-in key was removed from `settings.py`. The old key is still in the git history, so it must be
    rotated (Umut is generating a new one).
16. **Investment preferences and universe budget** (`feature/investment-preferences`). Preference form
    after the C-level offers only what the profile allows; Agent C's universe is capped at 50 instruments
    (25 single stocks, 10 crypto-assets), served round-robin in priority order. Single stocks: 180 large US
    names (up to 20 per industry), R4, 10% cap each. Crypto-eligible investors get the whole Binance USDT market
    ranked by 24h volume; tokenized shares (`<ticker>B`) and stablecoins are excluded. Gold-backed tokens
    (PAXG) count as gold but are rated R5. All PROVISIONAL.
17. **Investment directory and interest-driven fetching** (`feature/agent-c-dynamic-universe`).
    `market_intel/directory.py` lists every US-listed stock and ETF by NAME (Nasdaq stock screener: name,
    sector, industry, market cap, country; Nasdaq symbol directory: ETF names; Tiingo's free supported-tickers
    list: what Tiingo can price; one Tiingo IEX request: dollar volume) and sorts them into the interest-area
    clusters with fixed rules (no LLM). ~4,450 instruments; leveraged/inverse, options-strategy, volatility and
    ETN products are excluded. Rebuilt weekly into the cache (`python -m egoquant directory --refresh`);
    offline, Agent C uses the curated catalog, whose metadata and sectors win for the tickers it lists.
    `fetch_universe` walks the investor's clusters, most traded first; one that cannot be priced is replaced
    by the next of the same cluster. Caps: 25 per cluster, 50 single stocks, 30 crypto, 100 in total (Agent A
    takes ~20 s for ~130 instruments). Budget: 40 new Tiingo requests per build (cached prices are free);
    `TiingoUsage` keeps under 450 distinct Tiingo tickers a month. Crypto investors get the whole Binance
    market priced in parallel. Recent performance is NOT a ranking signal (it would be a forecast). The
    frontend asks the preference form at `/interests` (`POST /preferences/form`); `POST /portfolio` takes
    `preferences`. All PROVISIONAL; check the Nasdaq sources' terms against the "licensed data" rule.
    Only Treasury-bill and government money-market funds count as cash (Agent A treats cash as riskless);
    ultra-short and floating-rate credit funds are bonds (R2).
18. **Price fallback chain.** If a live feed fails (no key, key refused, blocked region, network error), Agent C
    switches the WHOLE run to the committed snapshot of REAL prices (`market_intel/snapshot.py`, labeled
    "real prices as of <date>, cached"; `EGOQUANT_PRICE_SNAPSHOT=off` disables it), then, only with
    `EGOQUANT_DATA_FALLBACK=synthetic` (off by default), to synthetic prices, labeled as such.
    `SnapshotReport.fallback_reason` and the API's `fallback_reason` (a safe label) say why. Sources are never
    mixed. A rate limit (429) is not a failure: Agent C stops asking that feed and keeps what it has. Exception:
    when a core fund or holding cannot be priced (429 and nothing on disk), the run switches to the snapshot
    BEFORE the cluster walk, so Agent A gets the full universe on snapshot prices, not just the 4 core funds
    (seen live on Oct 19: shared key used up, empty cache). A failing crypto feed drops only crypto. Tests: `tests/agent_c/test_data_fallback.py`, `test_price_snapshot.py`.
    **Two committed snapshots, one per reader** (`market_intel/data/`; a test checks who reads which):
    `prices_snapshot.json.gz` = Tiingo, 112 funds and crypto pairs, read by the **regime model** (the prices it
    was fitted and evaluated on; do not replace it). `prices_snapshot_yahoo.json.gz` = **Yahoo Finance**, all 292
    catalog tickers incl. the 180 single stocks, read by **Agent C's fallback** (`agent_c_snapshot_path`; the
    Tiingo file if it is missing). One run reads one file, so sources are never mixed. Build Agent C's with
    `snapshot --source yahoo --include-stocks` (`market_intel/yahoo.py`, ~30 s; team OK on Yahoo's terms, Oct 19).
    Offline builds only: yfinance is not in requirements.txt and the app never calls Yahoo (a test checks it);
    live builds stay on Tiingo. Checked against the Tiingo snapshot (same funds: median total-return gap 0.06%
    over 19 years; SPY daily-return correlation 0.99999; one bad print each: CPER on Yahoo, ITA on Tiingo). Agent C
    has no spike filter. A snapshot is used only up to `max_snapshot_age_days` (30) after its date.
19. **Goal-based portfolio choice** (`october18`). Inputs: start amount (B3), monthly contribution/withdrawal,
    goal amount, horizon (the app asks them after the questionnaire). Agent A builds exactly
    `candidate_target` = 100 candidates (risk levels x 5 styles; midpoints added until 100, thinned evenly
    above), Agent B keeps 5-20 (behavioral limits + comfort band), then a Monte Carlo of 10,000 paths per
    shortlisted portfolio: whole ~1-month blocks of REAL daily returns, fixed seed, the same paths for every
    portfolio. Choice: highest chance of reaching the goal; within `goal_tie_tolerance` (1%, above the ~0.5%
    standard error) the smallest worst-5% shortfall, then the lowest volatility. History: ~20 years
    (`goal_history_days` = 5040) so the sample holds 2008-09; ten strong years made every goal look certain.
    Younger funds are completed by Stambaugh (1997) / Page (2013): regression on the long-history funds plus
    resampled residual blocks (`goal.complete_history`), never by cutting everyone to the youngest start.
    Below `goal_min_history_days` (~5 years) no probabilities are shown; the portfolio is built the standard
    way and `goal_unavailable` says why. Agent C fetches the long history in the same requests
    (`fetch_universe(history_days=...)`, `get_price_history`). Walkthrough of every step with automatic
    checks: `python -m egoquant goal-demo <persona>`. All numbers PROVISIONAL.
    Two corrections against over-optimism, found on real data (98% chance and ~13%/yr before): the backfill
    drops the short-window intercept (zero alpha, Pastor & Stambaugh 1999), and `goal_hindsight_correction`
    (`goal.equilibrium_means`) sets each stock / equity fund / crypto-asset's AVERAGE return to beta x the VT
    market average over the same window (CAPM / Black-Litterman equilibrium prior), keeping its real daily moves.
    Reason: they are picked from today's most traded list (survivorship / look-ahead bias). Bonds, cash and
    commodities keep their own averages. After: growth persona 69% chance, median ~6%/yr.
20. **Crypto floor** (`october18`). An investor who chose crypto and may hold it (class crypto cap > 0, not
    knowledge-blocked, crypto in the universe) holds at least `interest_crypto_floor` (2%, PROVISIONAL) in every
    candidate and in the standard path: Agent A's lowest-risk styles otherwise left it out entirely. New
    constraint type `min_class_weight`; SOFT (a hard limit such as the stress loss or an Agent B limit wins
    and the floor is reported as relaxed); never above the class cap (5% for C5). The report shows it as
    "Crypto you asked for (minimum)". Tests: `tests/system/test_crypto_floor.py`.

21. **Cache warm-up** (`python -m egoquant warm-cache [--per-cluster 10] [--wait]`, `market_intel/warm.py`).
    Tiingo serves one ticker per request (50/hour, 500 distinct a month), and a build may spend only 40 new
    requests, so a cold cache gave most funds ~2 years. The job fetches the goal simulation's ~20-year window
    for core funds, then rank 1 of every cluster, rank 2, ... (round-robin, even coverage), skips cached
    tickers, waits on the hourly limit and stops at the monthly guard. Re-run when as_of moves on. On Railway,
    mount a volume at `EGOQUANT_CACHE_DIR` (the disk is wiped on redeploy).
    The SERVER also does this continuously (`warm.CacheWarmer`, started by main.py's lifespan): it works only
    while the process made fewer than `EGOQUANT_WARM_PER_HOUR` (30) Tiingo requests in the last hour,
    portfolio builds included, so traders keep the rest of the 50; it refreshes each new trading day;
    `EGOQUANT_WARM=off` disables it (tests switch it off in tests/conftest.py); off in synthetic mode or
    without a key. Progress: `GET /data/status`. One uvicorn worker per service (each process runs one).
22. **Two Tiingo keys.** `TIINGO_API_KEY` = traders (Agent C's fetches for each portfolio build);
    `TIINGO_WARM_API_KEY` = the background builder (warm-cache, CacheWarmer), 45 requests/hour on its own key.
    Both write to the SAME disk cache: a build uses cached funds for free and spends its budget on the cluster's
    uncached funds, and everything it fetches (always the ~20-year window) stays for later builds. Request
    counts and monthly ticker ledgers are kept per key (`tiingo_usage_<fingerprint>.json`, never the key).
    Keys live only in `.env` / the host's environment, never in the repo.
    **Since Oct 19:** without its own `TIINGO_WARM_API_KEY` the background builder does NOT start
    (`EGOQUANT_WARM=shared` runs it on the traders' key as before). Reason: its per-process request count restarts
    at 0 on every redeploy, so on one shared key it and the first builds used up the 50 requests an hour and traders
    got the snapshot only. After a 429, a key sends no request for `TIINGO_COOLDOWN_SECONDS` (15 min, PROVISIONAL),
    so builds switch to the snapshot at once. `GET /data/status` shows each key's fingerprint, its last 429 with
    Tiingo's own message (hourly, daily or monthly limit) and the cooldown left. Tiingo counts per ACCOUNT, so a
    key used on laptops shares the server's 50 an hour.

23. **Goal section of the investor's report** (`report.build_goal_report`, `ClientReport.goal`, HTML "Your goal"
    right after the summary; frontend tabs Goal / Outlook / Method, the goal notes first under Data, and
    "Download report" saves the HTML). Chance as a natural frequency + 100-icon array (Spiegelhalter et al. 2011),
    outcomes labelled by frequency ("1 in 10 futures ends below"), the size of a miss in money (worst-5%
    shortfall, falls along the way, money running out), numbers rounded (3 figures, whole percent, never a
    misleading 0%/100%), sampling error plus the older-half vs newer-half check (`goal.period_checks`), levers
    re-simulated on the same paths (`goal.goal_levers`: monthly amount for 50/75/90%, +2/+5 years, the goal reached
    in 50/75/90%), the method in plain words, and FINRA Rule 2214-style disclosures. Lever targets PROVISIONAL.
    The Advisor gets the same wording (`advisor/context._goal_facts`: chance as shown, outcomes, levers, history
    halves; prompt `advisor-v4`); `eval/chat_redteam.py` has two goal cases (`/debug/portfolio` takes an optional
    `goal_amount` / `monthly_flow`).

24. **What-If Agent** (`egoquant/agents/what_if.py`, tool `run_what_if` in `chat.py`, `POST /what-if` and
    `GET /privacy-log/{session_id}` in `egoquant/what_if_api.py`, migration `supabase/migrations/002_what_if.sql`).
    The advisor LLM only translates a hypothetical into a WhatIfScenario (extra_monthly_contribution,
    one_time_contribution, horizon_years, goal_amount, exclude_assets, mode; no risk class or caps) and explains the
    result. `/portfolio` returns a sealed `what_if_context` (never shown to the LLM; bound to the user for
    /account/portfolio). same_portfolio re-simulates the base weights; reoptimize re-runs the /portfolio code
    (`_execute`, no briefing, `WHAT_IF_SELECTION_PATHS`). Baseline and scenario share the same futures (common random
    numbers; same_portfolio's baseline equals the report's chance). The Risk Engine re-checks against the BASE hard
    limits; a failure returns status `blocked` with no numbers. Every advisor reply is number-grounded
    (`advisor/grounding.py`: regenerate once, then a fallback) and every OpenAI call goes through the redaction
    gateway (`advisor/redaction.py`). Prompt `advisor-v5`. PROVISIONAL: 10,000 evaluation paths, 3,000 selection
    paths, 20 s timeout, 10 runs per 10 minutes.

Questionnaire-specific cutoffs and their reasons: [egoquant/risk_profile/README.md](egoquant/risk_profile/README.md).

## Next builds

Eckel-Grossman risk game (feeds Agent B), then the questionnaire UI. Orchestrator wiring example:
[risk_profile README](egoquant/risk_profile/README.md).
