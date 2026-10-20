# Backend environment variables (Railway)

Every environment variable the backend reads, taken from the code on `october18` (`egoquant/settings.py`, `main.py`,
`chat.py`, `egoquant/advisor/context.py`, `egoquant/regime/hook.py`, `egoquant/briefing/*`, `egoquant/accounts/store.py`).
**No value is written here; the "Secret" column says which ones must never reach the repository, a log or a chat.**
A local `.env` file in the repository root is read too (gitignored); a variable that is already set in the environment
wins over `.env`. NB: a variable set to the EMPTY string counts as set, so it also wins over `.env`.

**What the live service runs (20 Oct, 13:00, from `GET /data/status`):** Python 3.14.4 (pandas 3.0.6, numpy 2.5.4, scipy 1.18.1), `EGOQUANT_PRICE_MODE=snapshot`, portfolio method v1, Risk Game weight one third (it may raise the category), the build gate at its default of 1, one math-library thread (`main.py` default). The secrets and the on-chain seal settings are set only in Railway; `GET /data/status` does not show any other variable, so this list is not a full inventory of what is set there.

"On/off" switches: a variable that is "off" with `off`, `0`, `false` or `no` (any case) is *on* for every other value,
including unset. The only exception is `EGOQUANT_REGIME_MC` (below), which is *on* only for `1`, `true`, `on`, `yes`.

## Prices: Tiingo, Binance, snapshots (Agent C)

| Variable | Default | Effect | Secret |
|---|---|---|---|
| `TIINGO_API_KEY` | empty | The *traders'* key: Agent C's fetches for every portfolio and trade-check build. Empty or refused (HTTP 401/403): the whole run switches to the committed snapshot, the report carries a "LIVE PRICES UNAVAILABLE" banner and `fallback_reason` is "Tiingo key not configured" / "Tiingo key rejected". After an HTTP 429 the key sends no request for 15 minutes (a code constant, `TIINGO_COOLDOWN_SECONDS`), so builds use the snapshot at once instead of waiting; `GET /data/status` shows the last 429 and the cooldown left. Tiingo counts per *account*: a key also used on a laptop shares the server's 50 requests an hour. | **yes** |
| `TIINGO_WARM_API_KEY` | empty | The background price-cache builder's own key (a second free Tiingo account). **Without it the builder does not start** unless `EGOQUANT_WARM=shared`, so it can never use up the traders' 50 requests an hour. | **yes** |
| `EGOQUANT_WARM` | on | `off` (also `0`, `false`, `no`) stops the background builder; `shared` lets it run on the traders' key when there is no `TIINGO_WARM_API_KEY` (30 requests an hour; not recommended: its count restarts at every redeploy). The builder is also off in synthetic mode and without any Tiingo key. `GET /data/status` shows its state and why it is off. | no |
| `EGOQUANT_WARM_PER_HOUR` | 45 with its own key, 30 shared | The builder sends requests only while fewer than this were made on its key in the last hour (this process's count). `0` = it never sends one. Not a number: the default. | no |
| `EGOQUANT_WARM_PER_CLUSTER` | 10 (minimum 1) | Funds per interest cluster the builder keeps warm. | no |
| `EGOQUANT_PRICE_MEMORY_FILES` | 250 (minimum 1) | Price files Agent C keeps in memory, least recently used dropped first; the disk cache keeps all. About 0.23 MB each, so 250 is about 58 MB. Lower it to save memory. | no |
| `EGOQUANT_CACHE_DIR` | `~/.cache/egoquant` | Where everything cached lives: per-feed price files (`tiingo/`, `binance/`), the monthly Tiingo ticker ledger (`tiingo_usage_<key fingerprint>.json`, never the key), `directory.json`, and `briefing/` (news archive, tone and summary caches, news-worker state). Point it at a mounted volume. If it cannot be written (read-only disk, bad path) the app logs one warning per kind and keeps prices in memory: slower, never an error. | no |
| `EGOQUANT_PRICE_MODE` | `live` | `snapshot` makes every build read the committed snapshot of real prices directly and send **no live request at all**: no Tiingo, no Binance coin list, no directory download, and the background price builder stays off. The report then says calmly "Prices: saved snapshot of real prices as of 16 Oct 2026, Yahoo Finance; crypto from Binance" (no "LIVE PRICES UNAVAILABLE"), `fallback_reason` is empty and the API carries `snapshot_first: true`. Any other value is `live`. If the snapshot is switched off, unreadable or older than 30 days, Agent C quietly uses the live feeds and says so in its notes. The natural choice when Tiingo's free quota (50 requests an hour, 500 tickers a month) cannot serve the traffic, and under portfolio method v2 (up to 292 products). | no |
| `EGOQUANT_LIVE_BUDGET_SECONDS` | 15 (minimum 1, PROVISIONAL) | In `live` mode: the most time one build may spend waiting for live price requests. Past it the WHOLE build switches to the committed snapshot (one source per run, the usual "LIVE PRICES UNAVAILABLE" banner and `fallback_reason`). Counts core funds and holdings too, and failed or timed-out requests. Only switches when a snapshot (or the opt-in synthetic fallback) can take over; with the snapshot off a slow answer is still accepted. | no |
| `EGOQUANT_SLOW_REQUEST_SECONDS` | 8 (minimum 0.5, PROVISIONAL) | A single live request slower than this is "slow"; two slow requests in one build switch the whole build to the snapshot, like the live budget above. | no |
| `EGOQUANT_PRICE_SNAPSHOT` | on | `off` disables the committed snapshot as a fallback: a failed live feed then gives a clean 503 ("Market data is unavailable right now"), or synthetic prices if `EGOQUANT_DATA_FALLBACK=synthetic`. | no |
| `EGOQUANT_DATA_FALLBACK` | unset | Only `synthetic` means anything: when the live feed *and* the snapshot fail, use invented prices, labelled DEMO. **Keep unset in production** (real data is never silently replaced). | no |
| `EGOQUANT_DATA` | unset (= live) | `synthetic` makes every build use invented prices and switches off the price builder and the news worker. **Must stay unset in production.** | no |
| Binance | no variable | Crypto spot prices come from the public `data-api.binance.vision`, no key. It answers HTTP 451 to servers in the US: then only the crypto pairs are dropped (reported in the notes) and every other price stays real. Prefer a non-US Railway region. | no |
| Snapshot paths | no variable | Fixed files in `egoquant/agents/market_intel/data/`: `prices_snapshot_yahoo.json.gz` (Yahoo Finance adjusted closes, 292 tickers, Agent C's first fallback) and `prices_snapshot.json.gz` (Tiingo, 112 tickers; the regime model reads it and Agent C uses it if the Yahoo file is missing or unreadable). Used for 30 days after the date inside the file. The report names the provider and that date. | no |

## Python version and portfolio method

| Variable | Default | Effect | Secret |
|---|---|---|---|
| (no variable) | `.python-version` = **3.14.4** | The Python that Railway builds and runs. Railway's Railpack builder reads `.python-version`; `RAILPACK_PYTHON_VERSION` (if set) overrides it, so leave that variable UNSET or set it to the same `3.14.4`. After a deploy, `GET /data/status` shows `python_version` (the Python the server really runs): it must start with `3.14`. | no |
| `EGOQUANT_PORTFOLIO_METHOD` | unset (= `v1`) | `v2` switches every build to portfolio method v2 (docs/PORTFOLIO_V2.md): all eligible products (up to 400), risk parity / hierarchical risk parity / maximum diversification / Black-Litterman / resampled candidates, 30% per-fund cap for C3-C5, the choice by least drawdown among the near-best. **Any other value, or unset, is v1.** Builds take longer under v2 (12-60 s of CPU; the proxy limit is 120 s). `GET /data/status` shows the active method as `portfolio_method`. | no |
| `EGOQUANT_RISK_GAME_WEIGHT` | unset (= one third) | The Risk Game's share of the attitude to risk (the other part is the questionnaire's level); the category is the lower of that blend and the capacity cap. `0` = the game is ignored everywhere (kill switch); values are clamped to 0..0.5; not a number = default. docs/RISK_GAME_BLEND.md | no |
| `EGOQUANT_RISK_GAME_MAY_RAISE` | on | `off` (also `0`, `false`, `no`): the game can only LOWER the tolerance part (category = lowest of the cap, the questionnaire level and the blend). Any other value or unset: it may also raise it, never above the capacity cap. | no |
| `EGOQUANT_MAX_CONCURRENT_BUILDS` | 1 (minimum 1; a junk value counts as 1; PROVISIONAL) | Portfolio builds (`/portfolio`, `/account/portfolio`, both trade checks, the chat's trade tool, a what-if re-optimisation) that run at the same time in this process; the others queue. Measured 20 Oct with 20 simultaneous heavy goal builds: one slot served all 20 in 89 s at about 300 MB, three slots took 130 s and turned 6 away, no gate used 3.2 GB and killed the server process in 3 of 3 runs (`docs/judges/04_TESTING_AND_EVIDENCE.md`). Railway has 1 GB: keep it at 3 or lower. | no |
| `EGOQUANT_BUILD_WAIT_SECONDS` | 120 | How long a build waits for a free slot before the answer is a plain 503 ("The service is busy building other portfolios") with `Retry-After: 30`. | no |
| `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS` | `1` (set by `main.py` before numpy is imported; a value already in the environment wins) | One math-library thread per process: the matrices are small, several builds at once would otherwise each start a full set, and concurrent native work hung or crashed the development PC's Windows stack. Float results (and so the golden hashes) do not depend on the machine's thread count. | no |
| `EGOQUANT_PRICE_MEMORY_FILES` | 400 | Price-cache files Agent C keeps in memory (about 60 KB each for 20 years, so 400 are ~25 MB). Only worth changing for a very small instance. | no |

The packages are pinned in `requirements.txt` (`eth-account==0.14.0` and its `ckzg 2.1.8` have wheels for Python 3.13 and 3.14 on Windows and
for 3.14 on Linux x86-64; they have none for 3.15, so run the tests in a 3.13 or 3.14 virtual environment, not 3.15).

## Goal simulation and sealed contexts

| Variable | Default | Effect | Secret |
|---|---|---|---|
| `EGOQUANT_REGIME_MC` | on | The goal simulation draws its futures from the regime sampler. Any value except `1`, `true`, `on`, `yes` (so `0`, `off`, `false`, and also an empty value) turns it off and uses the plain block bootstrap. Each report records which sampler ran. | no |
| `EGOQUANT_CONTEXT_SECRET` | derived from `OPENAI_API_KEY`, else random per process | HMAC key that seals the contexts the browser carries back (`chat_context`, `what_if_context`, `trade_context`, `briefing_context`) (maximum age 14 days). Set a long random value (32+ characters). Without it, every restart or redeploy invalidates all open contexts (users rebuild), and two instances would reject each other's. Changing it has the same effect once. | **yes** |

## Advisor chat and news (OpenAI)

| Variable | Default | Effect | Secret |
|---|---|---|---|
| `OPENAI_API_KEY` | empty | Advisor chat (`/chat`), the explanations of a trade check, the briefing's tone labels and summaries. Empty: ordinary chat answers 503 "Chat is not configured", a trade check is explained by its fixed template, and the news worker stays off. Set a spending limit on the OpenAI project (credit is exhausted at the time of writing). | **yes** |
| `OPENAI_MODEL` | `gpt-5.5` for chat | Chat model; the briefing's *summary* model reads the same variable (default `gpt-4.1-mini`). Tone labels use their own pinned, evaluated model and ignore it. | no |
| `OPENAI_MAX_OUTPUT_TOKENS` | 2000 | Cap on a chat answer. | no |
| `OPENAI_REASONING_EFFORT` | `low` | Reasoning effort for chat models that accept it. | no |
| `EGOQUANT_BRIEFING` | `live` | News briefing mode: `live` (real RSS), `fixture` (invented DEMO items; never in production), `off` (no briefing). An unknown value logs a warning and means `live`. | no |
| `EGOQUANT_BRIEFING_LLM` | unset (off) | `1`, `true`, `yes` or `on` lets the web path call OpenAI for tone labels and a few summaries (needs `OPENAI_API_KEY`; capped per request and per hour). Without it every headline is "tone not yet assessed". | no |
| `EGOQUANT_BRIEFING_TONE_BACKGROUND` | on | `off` stops only the after-response labelling of leftover headlines. | no |
| `EGOQUANT_NEWS_WORKER` | on | `off` stops the background worker that fetches feeds and rates headlines every 15 minutes. It also needs `EGOQUANT_BRIEFING=live`, `EGOQUANT_BRIEFING_LLM` on, an `OPENAI_API_KEY`, and `EGOQUANT_DATA` not synthetic. | no |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | unset | Optional Reddit source for the briefing; all three must be set, otherwise Reddit is skipped and the briefing says so. | `REDDIT_CLIENT_SECRET`: **yes** |

## Accounts and web

| Variable | Default | Effect | Secret |
|---|---|---|---|
| `SUPABASE_URL` | empty | The Supabase project. With the key below unset, `/account/*` answers 503 "Accounts are not configured" and everything else works. | no |
| `SUPABASE_ANON_KEY` (alias `SUPABASE_KEY`) | empty | The **anon** key only. A `service_role` key is refused (accounts answer 503) because it would bypass row-level security. | **yes** |
| `CORS_ORIGINS` | `https://fintechaton-frontend.vercel.app` | Comma-separated browser origins allowed to call the API; no credentials. Set it if the Vercel domain differs. | no |
| `EGOQUANT_DEBUG_PAGES` | unset (= off) | `on` (also `1`, `true`, `yes`) enables `POST /debug/portfolio` (demo personas on synthetic prices). Any other value, or unset, makes it answer 404. **Keep unset in production.** Only the tests and `eval/chat_redteam.py` / `eval/chat_regime_redteam.py` use it, in-process; they turn it on themselves. Nothing the app or the judges' demo uses depends on it. The frontend has the same variable on Vercel for its `/debugmodels` page. | no |
| `PORT` | 8000 | Set by Railway; the `Procfile` runs `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`. | no |

(The frontend on Vercel has its own variables: `BACKEND_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, and
`EGOQUANT_DEBUG_PAGES`: unset, the `/debugmodels` page and `/api/debugmodels` answer 404; keep it unset in production.)

## Railway checklist

1. **Volume.** (20 Oct: the live service has none attached, so the cache is wiped on every deploy; with `EGOQUANT_PRICE_MODE=snapshot` that does not matter.) Mount a volume and set `EGOQUANT_CACHE_DIR` to it (for example `/data/egoquant`). Without one the cache is lost on every redeploy (it rebuilds from Tiingo, a few requests at a time).
2. **Memory.** (20 Oct, read from the Railway dashboard: the live service has 1 GB RAM and 2 vCPU on a trial plan; with the build gate at its default of 1 a heavy build peaks near 300 MB.) The design target is 512 MB. On the real 292-ticker universe (Yahoo snapshot as the fallback), six personas with a goal of 10 and of 30 years, one process, the peak was 298 MB and 10 to 23 s per request (idle about 180 MB; the first request loads the 9 MB snapshot, about 100 MB for a moment). Longer horizons and two requests at once, measured offline on the frozen price cache (`eval/memory/horizon_memory.py`, results in `eval/results/memory_horizon.md`): idle about 170 MB, one goal request up to about 400 MB at 60 years, but **two goal requests at the same moment at 60 years reach about 500 MB**. If Railway shows memory kills, raise the limit to 1 GB first; lowering `EGOQUANT_PRICE_MEMORY_FILES` saves little.
3. **One worker, one instance.** Rate limits, the price cache and the background builder live in the process. Run a single `uvicorn` worker and do not scale out (a second instance would also need the same `EGOQUANT_CONTEXT_SECRET`).
4. **Region.** Choose a non-US region if crypto prices matter (Binance answers 451 to US servers).
5. **Must be set:** `TIINGO_API_KEY`, `EGOQUANT_CONTEXT_SECRET`, `EGOQUANT_CACHE_DIR`; `OPENAI_API_KEY` for chat; `SUPABASE_URL` + `SUPABASE_ANON_KEY` for accounts; `CORS_ORIGINS` if the site domain is not the default.
6. **Should be set:** `TIINGO_WARM_API_KEY` (a second free key from a *different* Tiingo account; without it the builder stays off and the cache only grows through traders' builds); `EGOQUANT_BRIEFING_LLM=1` if the News tab should show AI tone.
7. **Must stay unset:** `EGOQUANT_DATA` (synthetic), `EGOQUANT_DATA_FALLBACK`, `EGOQUANT_BRIEFING=fixture`, any `service_role` key in `SUPABASE_*`.
8. **Snapshot-first mode.** If you cannot rely on Tiingo's free tier (50 requests an hour, 500 distinct tickers a month; one slow or rate-limited request costs a user up to 15 s on Railway), set `EGOQUANT_PRICE_MODE=snapshot`: no live request is made and prices are those of the committed snapshot (292 products, 16 Oct 2026). It is used for 30 days after its date (until 15 Nov 2026); rebuild it before then with `python -m egoquant.agents.market_intel.snapshot --as-of <last close> --source yahoo --include-stocks` (offline, needs `pip install yfinance`), or Agent C quietly returns to live prices.
9. **After deploy:** `GET /data/status` shows the builder, the traders' key usage, the settings in force and the library versions (`libraries`: pandas, numpy, scipy); `eval/stress/probe_after_deploy.py` makes 18 read-only checks of the hardening; open a portfolio and check the Data tab and banners say real prices (no "LIVE PRICES UNAVAILABLE"). Then run the deploy check below once.

## After a deploy: run the check

`eval/deploy_check.py` asks the deployed API the questions this document is about and prints PASS, FAIL or WARN per check. It is read-only (no
account, no key, no token, no AI call, nothing stored, no trade) and needs only plain Python 3 (nothing to install). Run it once from a copy of the repository:

```
python eval/deploy_check.py https://<railway-backend>            # the whole check, about 2 to 3 minutes
python eval/deploy_check.py https://<railway-backend> --quick    # a few seconds: no portfolio is built
```

**Order: deploy the backend first, run the check with `--quick`, then deploy the frontend.** The Risk Game page sends the result inside
`answers.risk_game`; a backend that is older than that answers 422 to every request from the new frontend, and the check's Risk Game step
(`POST /profile` with and without a game) says so in seconds.

Exit code 0 = passed, 1 = at least one FAIL. Options: `--method v2` if v2 is the default, `--python 3.14` (the version it expects), `--news-worker off` (the state
you expect), `--origin <the web address>` if `CORS_ORIGINS` is not the Vercel default, `--numbers <file>` to compare with another demo pack. The full run builds
five portfolios and three trade checks only when the server is in snapshot mode; on any other price mode it prints `WARN portfolio builds skipped` (building would send live
Tiingo requests with the production key) and fails on the mode itself. `--allow-live` builds anyway; normally do not.

What a FAIL means, in one sentence each:

| FAIL line starts with | It means |
|---|---|
| `price_mode is snapshot` | The server still asks live price feeds; set `EGOQUANT_PRICE_MODE=snapshot` and restart (the builds are skipped until then). |
| `Python version starts with` | Railway runs another Python than the one we tested; fix the service's Python version, then redeploy. |
| `portfolio_method is` | The server builds portfolios with the other method than the demo script describes; set or unset `EGOQUANT_PORTFOLIO_METHOD` (or run the check with `--method`). |
| `news worker state is` | The news worker is not in the state you said you expected; `WARN news worker rates headlines` means every rating failed, usually because the OpenAI credit is gone. |
| `GET /data/status`, `GET /docs`, `GET /openapi.json` | The API does not answer there: wrong address, the service is not running, or it crashed on start; read the Railway log. |
| `security headers` | A protective header is missing, so something other than `main.py` answers or a proxy strips headers. |
| `CORS` | The web page may be refused by the browser (our origin not allowed) or a foreign origin is allowed; fix `CORS_ORIGINS`. |
| `POST /profile` ... `with the Risk Game` | The backend does not know `answers.risk_game` (an old deploy: it answers 422) or scores another category with the game than the demo pack; deploy the backend before the frontend. |
| `POST /profile` ... `without the game` | A profile without a game carries game fields or another category than the questionnaire alone, so the old no-game behaviour changed. |
| `Risk Game weight`, `Risk Game may raise` (WARN) | The server runs another game weight than 0.3333 or may not raise the category, so categories can differ from the demo script; check `EGOQUANT_RISK_GAME_WEIGHT` and `EGOQUANT_RISK_GAME_MAY_RAISE`. |
| `POST /preferences/areas` | The investment page cannot show which interest areas will be used. |
| `... answers within 30 s` | A portfolio build or trade check failed or took longer than 30 s, so a user would wait too long; read the log line and the time printed. |
| `category`, `approved`, `every Risk Engine check passes` | The engine answers differently from the demo pack: other code, method or limits than the demo script assumes. |
| `goal chance`, `stock share`, `volatility ... of the pack` | The numbers differ from the demo script by more than the decision-date effect (about 1 point), so the script would not match the screen. |
| `saved-prices note is shown` | The portfolio does not say that its prices are saved ones; judges could not tell the date of the prices (a WARN means the amber fallback banner showed instead of the calm snapshot note). |
| `Agent B's behavioural limit` | A trading history no longer tightens the limits as in the demo. |
| `banner says the first 8 of 20 areas were used` | A request with 20 interest areas is not shortened or not labelled as shortened. |
| `... verdict is the pack's` | A trade check gives another verdict than the demo script promises. |
| `what-if` | The sealed what-if context is not handed out, or the what-if route answers without a sign-in. |

Tolerances (PROVISIONAL, `TOLERANCES` at the top of the script): goal chance 3 points, stock share 2, volatility 0.5, Agent B limit 0.3; moving the decision date by three days changed the goal chance by at most one point.
The category, the approval, the Risk Engine's passed/total and the trade verdicts must be equal. Tests: `tests/test_deploy_check.py`.

## Kill switches (exact)

| To stop | Set | What happens instead |
|---|---|---|
| The background price builder | `EGOQUANT_WARM=off` | Builds use the cache plus at most 40 new requests each |
| Live Tiingo for traders (quota gone, vendor down) | `TIINGO_API_KEY=` (empty) | Every build uses the labelled snapshot (a 429 does this by itself for 15 minutes) |
| The saved-price fallback | `EGOQUANT_PRICE_SNAPSHOT=off` | A failed live feed gives a clean 503 |
| All live price requests (quota gone, vendor unreliable) | `EGOQUANT_PRICE_MODE=snapshot` | Every build reads the committed snapshot directly, labelled calmly, zero requests |
| Slow-vendor waiting | `EGOQUANT_LIVE_BUDGET_SECONDS=8` | The build gives up on a slow feed sooner and uses the snapshot |
| The regime sampler | `EGOQUANT_REGIME_MC=0` | Plain block bootstrap |
| Many simultaneous builds (memory, native crashes) | `EGOQUANT_MAX_CONCURRENT_BUILDS=1` | Builds run one at a time; others wait up to `EGOQUANT_BUILD_WAIT_SECONDS`, then a plain 503 |
| News briefing entirely | `EGOQUANT_BRIEFING=off` | No News tab content |
| AI news tone and summaries | unset `EGOQUANT_BRIEFING_LLM` (or `0`) | Tone "not yet assessed", template summaries, worker off |
| The news worker only | `EGOQUANT_NEWS_WORKER=off` | Briefings still build on request |
| The advisor chat | unset `OPENAI_API_KEY` | `/chat` answers 503 |
| Accounts | unset `SUPABASE_URL` | `/account/*` answers 503 |

A changed variable takes effect after a restart (Railway restarts the service when a variable changes).
