# Agent D: Market Briefing (read-only) (`egoquant/briefing`)

News about the investor's instruments, shown **alongside** the portfolio. It explains what sources are
saying; it never changes the portfolio.

**Scope:** Agent D briefs on the approved portfolio and the investor's interests only, so it scales with catalog size.
It covers the funds with weight > 0 in the final proposal (largest first), other held assets, and the funds in the
investor's interest areas including Agent C's watchlist, capped at `live_sources.max_live_tickers` (15). Interest
areas share the remaining slots round-robin, so every area gets at least one; the tickers the cap left out are listed
in the briefing metadata. It does not cover the rest of Agent C's universe.

**Sources:** News feeds are free, non-commercial use; we store only headline, link and date; the collected CSV is gitignored.

**Tone:** fund-aware. For each headline and each fund it concerns, gpt-4.1-mini answers one question: for an
investor who **holds this fund**, is this headline likely good, bad or neutral/unclear news? On 120 headlines labeled
by our team for the matched fund, this matched the human label **85%** of the time; FinBERT, which only reads the
wording, 53% (`eval/results.md`). FinBERT (local, free) is the automatic fallback. Details below.

**Cost:** about $0.0001 per (headline, fund) pair, cached forever and shared across investors, capped per run
(`tone.llm_max_calls_per_run`). Summaries are a separate, cached and capped OpenAI use. No paid news or
sentiment API is used; the Tiingo news adapter is a disabled stub because it needs a paid plan.

## How Agent D works

(The same text is shown, collapsed, at the top of the News tab.)

1. **Collects:** every 15 minutes it reads free news feeds for the funds you hold and the areas you follow
   (headline, link and date only; never full articles).
2. **Cleans:** removes duplicates, automated pages and items without a reliable date.
3. **Links:** connects each headline to the funds it is about (ticker, fund name or topic).
4. **Rates:** an AI model answers one question per headline and fund: is this good, bad or neutral news for
   someone who holds this fund? On 120 headlines labelled by our team it agreed with the human label 85% of the
   time (a standard model, FinBERT, 53%).
5. **Scores:** per fund, a weighted average from −1 to +1 (trusted and recent sources count more).
6. **Compares:** this week vs last week (or month vs month).
7. **Reports:** if a fund you hold moves by 0.5 or more, it offers a risk-profile re-check. Nothing changes unless
   you confirm.

It never buys, sells or changes your portfolio, never forecasts prices, and sends no personal data to the AI.

### Rating every headline: background worker and briefing jobs

- **Background news worker** (`worker.py`, started by `main.py` like Agent C's price-cache warmer):
  - every `news_worker.interval_minutes` (15) it fetches the feeds for the catalog's core funds and every fund
    in any interest area (US-listed, at most 120), plus every area's topic searches and the general feeds;
  - it appends the headlines to the news archive and rates every new (headline, fund) pair with the unchanged,
    hash-pinned prompt, 10 at a time, newest first;
  - caps: `labels_per_run` 1500 and `labels_per_day` 3000 (about $0.30 a day);
  - switch: `EGOQUANT_NEWS_WORKER=off`. It is also off in fixture/off mode, without `EGOQUANT_BRIEFING_LLM=1` and a
    key, with synthetic prices, and in tests;
  - its state (last run, items fetched, pairs rated, failures, labels today) is in `GET /data/status` →
    `news_worker`.
- **Briefing jobs:**
  - `/portfolio` returns the portfolio plus a sealed `briefing_context` (HMAC with the advisor's secret). It
    holds the APPROVED weights (none when rejected), interests, watchlist, max R-level, horizon and Agent C's
    drawdown numbers; client-supplied weights are never used.
  - `POST /briefing/start {briefing_context}` returns a job id. `GET /briefing/{job}` returns `{state, stage,
    rated, total}`, and when done the week and month briefings plus "What changed" for both.
  - A job fetches every feed for the investor (about 140: per-ticker Yahoo, Nasdaq, Google News and Seeking
    Alpha for up to 30 tickers, Google News by each held fund's name, the topic searches, and MarketWatch, CNBC
    and PR Newswire), adds the archive, and rates EVERY relevant pair missing from the cache, holdings first,
    20 at a time, with retries.
  - `max_seconds` 150 and `max_pairs` 1500 are only a safety net; leftovers go to the background and show as
    "rating in progress". It never re-runs the portfolio build, and it has its own rate limit.
- **FinBERT** (only where it is installed and downloaded, i.e. local Macs) fills pairs the LLM could not rate,
  marked "FinBERT".
- **Measured on 18–19 Oct 2026, live news, empty cache:**
  - **before:** C3 0 of 42 pairs rated this week (0 of 117 this month); VNQ profile 7 of 18 (7 of 83);
  - **after:** C3 466 of 466 (887 of 887); VNQ profile 625 of 625 (1070 of 1070), in 40–50 s per job.

### Optional sources

- **SEC EDGAR fund filings:** set `SEC_USER_AGENT` (a descriptive User-Agent with contact details, for example
  `EgoQuant demo you@example.com`; the SEC requires it). Briefing jobs then add recent filings of the held funds'
  trusts (up to 10 funds).
- **Reddit (off):** needs `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `REDDIT_USER_AGENT`. To get them, create a
  "script" app at https://www.reddit.com/prefs/apps while signed in. Forums count 0.4 and are never needed.

## News tone: method, evaluation, fallback, privacy

- **Method** (`tone.method`, PROVISIONAL): `llm_fund_aware` (default) or `finbert`. With `llm_fund_aware`, each
  (headline, fund) pair gets a label from `fund_tone.py`; label → value: positive +1, neutral 0, negative −1.
  A headline about two funds can be good for one and bad for the other (e.g. rising yields: bad for long-bond
  holders, barely relevant for T-bills).
- **Prompt:** exactly the evaluated one, `prompts/fund_aware_tone.txt`, byte-identical to the file fixed and
  committed in `1adb563` before the evaluation run (a test checks its SHA-256). Never edit it: a new prompt needs
  a new evaluation. The evaluation scripts read the same file. Model gpt-4.1-mini, temperature 0, `store=False`.
- **Evaluation:** 120 headlines, labeled twice by our team for the matched fund, 30 disagreements adjudicated.
  Accuracy 0.850 / macro-F1 0.852 (FinBERT 0.533 / 0.511; Loughran–McDonald 0.375 / 0.320). With 120 headlines the
  95% interval is about ±6 points. Full table: `eval/results.md`.
- **Fallback to FinBERT** (gated P(pos) − P(neg), local, free) for a pair when: no `OPENAI_API_KEY`, `llm.enabled`
  is false, the per-run cap is reached, an API/network error occurred (no further calls that run), or the answer
  was not exactly one of the three words ("unscored"). Cached labels are always used (they cost nothing and send
  nothing). Every pair records its method; the report shows the counts per method and why fallback happened.
- **Privacy:** the request holds only the headline (untrusted: angle brackets replaced) and ONE fund (ticker and
  plain-English name). Never investor data, weights or ids. The cache key is (headline id, ticker, prompt hash,
  model), shared across all investors, so neither a request nor the cache reveals whose portfolio a fund is in.
- **Display:** per fund, words + number + count: "mostly negative (−0.40, 5 headlines)", using fixed bands
  (`tone.display`, PROVISIONAL): ≥ +0.5 mostly positive, +0.15..+0.5 slightly positive, between mixed or neutral,
  −0.5..−0.15 slightly negative, ≤ −0.5 mostly negative. Each headline shows "negative for holders of TLT"
  (FinBERT fallback: "negative wording, TLT (FinBERT)"). The section opens with `tone.header_note`.
- **Web app** (`web.py`): the fund-aware labels come from the shared cache (`fund_tone_cache.json`), then the
  FinBERT cache file (FinBERT itself is not installed on the server). A pair with neither shows **"tone not yet
  assessed"**: it is left out of the tone scores and alerts, and the section counts it ("… AI-assessed for
  holders, … FinBERT fallback, … not yet assessed"). Live calls on the web path happen only with
  `EGOQUANT_BRIEFING_LLM=1` and an `OPENAI_API_KEY`: at most `WEB_TONE_CALLS` (8) uncached pairs per request,
  most recent first, in parallel, inside what is left of the request's single 5-second budget
  (`TOTAL_FETCH_SECONDS`, shared with the feed fetch); per-call timeout 2 s, no retry, nothing started with
  under 1 s left. Late or failed calls are abandoned and leave the pair "not yet assessed"; answers that arrive in
  time are cached for every investor. Without those two settings the web path is cached-only.

## Briefing period: week or month

`build_briefing(..., briefing_period="week" | "month")` (CLI `--period week|month`; default
`default_briefing_period: week`) picks the window the briefing summarises and compares:

| Period | This period | Lookback (this + previous) | Recency half-life | Text |
|---|---|---|---|---|
| `week` (default) | 7 days | 14 days | 3 days | "this week" / "last week" |
| `month` | 30 days | 60 days | 10 days | "this month" / "last month" |

The preset only sets `window.period_days`, `window.lookback_days` and `tone.recency_half_life_days` (on a copy
of the config). Everything else is the same for both, including `tone.min_items_for_aggregate` (3): with fewer
than 3 headlines in the previous period the line says "not enough coverage last month to compare" and no
change, so no alert, is computed. `Briefing.briefing_period` records which preset was used.

**Cadence is separate.** `cadence` (from the investor's horizon: weekly, or daily for short horizons) says how
OFTEN a briefing would be sent; `briefing_period` says WHAT WINDOW one briefing covers. A weekly-cadence investor
can still open a monthly view, and the two settings never change each other.

### News archive (monthly history)

RSS feeds only return recent items, so a live monthly view needs history. Every live run adds the fetched items
to `news_archive.json` in the briefing cache directory (`settings.cache_dir()/briefing`, outside the
repository; `briefing_cache/` is gitignored if the cache directory is set inside it). Entries are deduplicated
by item id and never overwritten. Each entry keeps only the headline, link, publication date, source name and
type, matched tickers, when it was first fetched and whether it was live or sample data: **never an article
body or feed summary**. A live briefing reads the archive (items published within the lookback) plus the
fresh items; fresh items come first, so they win the dedupe and keep their feed summary. Until the archive has a
month of history, the monthly view says "not enough coverage last month to compare". The web endpoint
(`web.py`) does not read or write the archive yet. The offline fixture has invented items dated 15–60 days
before its `as_of`, so `--period month` shows a real comparison without any network access.

## Sources, dates and the News tab structure

**Real publishers.** `source_name` is the real publisher, `feed` is where we got the item:
- Yahoo items are named by their link's domain (`sources.publisher_domains`, e.g. 247wallst.com is 24/7 Wall St.);
- Nasdaq items by the feed's `<author>` (The Motley Fool, BNK Invest);
- Google News items by the entry's `<source>` element, with the " - Publisher" title suffix removed (also when
  the publisher itself contains " - ", as in "U.S. News - Money").

Item ids are unchanged, so existing caches still apply.

**Source tiers** (`sources.tiers`, PROVISIONAL, one-line reason per source, from the 14-day audit of 18 Oct 2026):

| Tier | Label | Tone weight |
|---|---|---|
| tier1 | established (wires, newspapers, financial media, regulators, issuers' releases) | 1.0 |
| tier2 | commentary (named-author analysis) | 0.7 |
| unrated | not reviewed yet | 0.4 |
| tier3 | automated or low-quality (template, quote, flow-alert, holding-history pages) | not used |

Forums keep 0.4. `sources.tier3_title_patterns` make any headline tier3, whatever the source ("ETF Inflow Alert",
"Holding History", "Options Chain", "What did … say for …"). Tier3 items are listed under "low-reliability
sources, not used for tone", are never sent to the LLM, and never count for tone or alerts. Aggregation weight is
tier weight × recency.

**Dates.** The feed's own date is used, never the fetch time. Each item has a `date_status`:
- `published`: the feed's pubDate;
- `updated_only`: only an `<updated>` date exists; it is shown as "updated 9 Oct";
- `unknown`: the item is kept and shown as "date unknown", counted in `n_date_unknown`, and excluded from tone
  windows.

A date more than 15 minutes after fetch time is rejected. Article pages are never fetched. The audit found the
feed date equal to the stored date for 30 of 30 items, and within 20 minutes of the page's own date for 17 of the
18 readable pages.

**Capital-only tickers.** SHY, BIL, TIP and AGG (`capital_only_tickers`) match a bare token only in capitals:
- "BIL: ETF Inflow Alert" and "Noteworthy ETF Inflows: SHY" match;
- "Investors Shy Away", "shy", "SHY-looking" and all-capital headlines do not.

Only US exchange tags count ("(NYSEARCA:SCHD)"). Non-US listings ("(ASX:IVV)", "LSE:VUSA", "VXUS.DE") are removed
and never match the US fund.

**Structure for the News tab** (all fields are additive; older readers keep working):
- **Instrument aggregates:**
  - one per holding with weight > 0, even with no news, plus other covered instruments that had news;
  - fields: `held`, `weight`, `name`, `r_level`, `areas`, and `coverage_status`. The status is a tone, or
    `not_enough_coverage` (n headlines; needs 3), or `not_yet_assessed` (n waiting). It is never "not yet
    assessed" when any headline is assessed.
- **Area aggregates:**
  - only the areas the investor chose, also without news;
  - `instruments` lists each one as held / not_held / above_risk_level, with its R-level;
  - `parent_area` nests overlapping areas (U.S. stocks under Stocks).
- **One card per headline.** `headline_ids` says which headlines a card SHOWS. Each headline of the period is
  shown once:
  - under the largest holding it concerns, otherwise under the most specific chosen area;
  - every other card it concerns counts it in `shown_under` / `n_shown_under_holdings` ("+N shown under your
    holdings"). Tone still counts every relevant headline.
- **Top level:**
  - `period_words`, `alert_threshold`, `holdings_order`, `chosen_areas`;
  - `tier_counts`, `method_counts` (AI-assessed / FinBERT / not yet assessed), `low_reliability_items`;
  - `single_tier_notes` ("All headlines about SHY this week come from commentary sites.");
  - `largest_move` (for the empty "What changed" state) and `tier_legend`.
  - The old "balance your sources" suggestions are retired (`diversity_suggestions` is always empty).
- **Both periods from one fetch.** The web briefing builds the week and attaches the month (`Briefing.month`).
  `/portfolio` returns them as `report.market_briefing` and `report.market_briefing_month`, plus `what_changed`
  and `what_changed_month`. There is no extra endpoint and no second fetch; the period toggle is client-side.

## Rules (enforced in code and tests)

| Rule | Where |
|---|---|
| Never changes Agent A's inputs or weights, Agent B's limits or the Risk Engine's decision | `build.py` only reads its inputs; `test_briefing_never_mutates_its_inputs`, `test_report_with_briefing_keeps_every_number` |
| The only action is an alert that **offers** a risk-profile re-check | `models.AlertAction = Literal["offer_risk_profile_recheck"]` |
| No buy/sell/hold advice, no forecasts | `advice_filter.py` on every LLM summary, every template and every rendered line |
| Every number (tone, exposure, counts, dates) comes from deterministic code | `aggregate.py`, `matching.py`. The only model output used is a per-(headline, fund) label (positive / negative / neutral), mapped to +1 / 0 / −1 by config; weights, decay, coverage minimums, exposure and alert rules are code. The summary's `llm_tone` and `mentions` are stored for evaluation and logging only |
| The LLM never decides anything about the portfolio | The fund-aware label only feeds the tone aggregates and the alert that **offers** a risk-profile re-check; it never sets a score, weight, limit or approval |
| Labels on every briefing | "AI-generated summaries", "News tone is a measurement of what sources say, not a forecast", "Does not change your portfolio", plus source and date per item |
| Sample data is labeled | Fixture briefings carry the banner **"DEMO: sample news items, not real news."** at the top of the report |
| No investor data to the LLM | `summarize.LLMRequest` has only `source_name`, `title`, `text`; `test_no_investor_data_in_the_llm_request` |
| Article text is untrusted | System prompt says it is data; structured output plus Pydantic validation; filter afterwards |
| Keys only from the environment | `OPENAI_API_KEY`, `REDDIT_*`, `SEC_USER_AGENT`; `.env` is gitignored; `.env.example` has names only |
| Copyright | Only headline, the feed's own summary/description and the link are kept. No scraping and no full articles |
| Tests and the offline demo make no network, model-download or OpenAI calls | Fake scorer and fake LLM in tests; the demo reads committed caches |

## Flow

```
sources ──fetch.py──> NewsItem (title, feed summary, link, date, source)    RSS · Reddit · SEC EDGAR · fixture
        ──prepare──>  deduplicate (URL, near-identical titles) · time window
        ──matching.py──> catalog tickers + interest areas (Agent C catalog; cashtags, whole words, keywords)
        ──fund_tone.py──> fund-aware label per (headline, fund): +1 / 0 / −1   ← primary tone (cached, shared)
        ──tone.py──>  FinBERT tone = gated P(pos) − P(neg) (local, cached)    ← fallback
        ──summarize.py──> 2–3 neutral sentences (OpenAI, cached, capped) → advice filter → else template
        ──aggregate.py──> weighted, recency-decayed tone per instrument and area · change vs last period ·
                          exposure (Agent A weights) · alerts · R-level notes · source diversity · cadence
        ──Briefing──> report.py "Agent D: Market Briefing (read-only)" section (optional `briefing=` / `attach_briefing`)
                      + "What changed": Agent C drawdown alerts (holdings first, then interest/watchlist
                        instruments; the rest of the universe as one count line) + Agent D tone alerts, built
                        from the numbers, advice-filtered; only action offer_risk_profile_recheck

Alerts state both levels and the change: "News tone about VNQ moved from mostly positive (+0.82) last week to mostly
negative (-1.00) this week (change -1.82, 7 headlines). It concerns 7% of your portfolio. …". An area alert is not
shown when an instrument alert is driven by exactly the same headlines, or when every held instrument in the area
already has its own alert. The header names what actually scored the items, e.g. "Tone: fund-aware gpt-4.1-mini
(23), FinBERT fallback (0); summaries: gpt-4.1-mini (brief-v1) or a fixed template."

In the pipeline: `run_pipeline(..., briefing=True, briefing_period="week"|"month")` or `briefing_builder=` (e.g.
`egoquant.briefing.__main__.make_builder(live=False, allow_llm=False)` for the offline sample). The briefing is built
after the Risk Engine verdict; a rejected proposal sends no weights (no exposure, no tone alerts). With `--synthetic`
or `--csv`, `advise --briefing` uses the DEMO sample news; with real prices it uses live news.
```

Product R-levels, tickers and interest areas come from Agent C's catalog (`agents/market_intel/catalog.py`).
This module keeps no second list. `instrument_keywords` only adds search words, keyed by catalog ticker, and a
test fails if a key is not a catalog ticker.

## Running

```bash
pip install -e ".[dev,briefing]"            # core + feedparser + openai
pip install -e ".[nlp]"                     # optional: FinBERT (torch + transformers, large)

python -m egoquant.briefing                 # OFFLINE demo: sample items + synthetic prices -> reports/briefing_demo.html
python -m egoquant.briefing --no-llm        # same, template summaries only
python -m egoquant.briefing --live          # live RSS + Reddit (+ EDGAR if enabled); FinBERT if installed
python -m egoquant.briefing --period month  # monthly view (this month vs last month) -> reports/briefing_demo_month.html
python -m egoquant advise growth_investor --synthetic --briefing --period month   # full pipeline + briefing + "What changed"
pytest -q tests/briefing                    # offline
```

The offline demo uses the `growth_investor` persona, with crypto and bonds added to its interests, on
synthetic prices. It reads `data/fixture_scores.json` (FinBERT scores produced once, locally) and
`data/fixture_llm_cache.json` (summaries produced once with gpt-4.1-mini: 50 calls, 17,733 input and 2,615
output tokens). Items without a cached summary, or whose cached summary fails the advice filter, show the
template. In the sample data that includes the prompt-injection post: the model refused to follow it but
repeated the injected advice, so the filter replaced its summary.

Maintenance (writes the committed caches; only invented fixture text is processed):

```bash
python -m egoquant.briefing --refresh-fixture-scores      # needs .[nlp]; downloads ProsusAI/finbert once (~440 MB)
python -m egoquant.briefing --refresh-fixture-fund-tones --max-calls 80   # fund-aware labels for the sample items
python -m egoquant.briefing --refresh-fixture-summaries --max-calls 50   # needs OPENAI_API_KEY; cached
```

### Deployed app (Railway)

The `Procfile` runs `uvicorn main:app`, which serves the chat endpoint and the engine routes
(`egoquant_routes.py`). `/portfolio` attaches the briefing through `web.build_web_briefing`. `torch` is
deliberately not in `requirements.txt` (too large), so **FinBERT never runs on the server**. Tone there comes
from the cached fund-aware labels, then the FinBERT cache file, then "tone not yet assessed". The local demos
(`python -m egoquant advise … --briefing`, `python -m egoquant.briefing`) run fully offline on the sample data.

**Railway's filesystem is temporary.** Everything under `EGOQUANT_CACHE_DIR` (default `~/.cache/egoquant`) is
lost on every deploy or restart:

- **The fund-aware tone cache** (`briefing/fund_tone_cache.json`): uncached headlines are labelled again, at
  most 8 calls per request. That costs a few cents at most, about $0.00008 per call at gpt-4.1-mini list prices.
  Until then the headlines show "tone not yet assessed".
- **The news archive** (`briefing/news_archive.json`): the web path does not use it yet anyway. A monthly view
  on the web would say "not enough coverage last month to compare" until about two months of history exist in
  one container.
- **The summary and FinBERT caches.**

To keep them, mount a Railway volume and point `EGOQUANT_CACHE_DIR` at it. That is optional; nothing breaks
without it.

Railway variables (safe defaults first):

| Variable | Safe default | Effect |
|---|---|---|
| `EGOQUANT_BRIEFING` | `fixture` for a judged demo (DEMO-labelled sample news, no network); `live` for real feeds; `off` to hide it | Unset means `live` |
| `EGOQUANT_BRIEFING_LLM` | unset (off) | `1` allows OpenAI calls on the web path: up to 8 tone labels and 5 summaries per request, inside the 5-second budget. Unset means cached labels and templates only, so no OpenAI cost |
| `OPENAI_API_KEY` | set as a Railway secret, never in the repo | Needed only with `EGOQUANT_BRIEFING_LLM=1` (and by the chat endpoint) |
| `OPENAI_MODEL` | unset (`gpt-4.1-mini`) | Summary model only. The tone model is fixed by `tone.llm_model` (the evaluated model) |
| `EGOQUANT_CACHE_DIR` | unset, or a mounted volume path | Where the caches and the archive live |

The web caps and timeouts are code constants in `web.py`, not environment variables. Change them in code,
with review:

- feeds and tone calls share one 5-second budget (`TOTAL_FETCH_SECONDS`);
- tone: 8 calls per request (`WEB_TONE_CALLS`), 2 s per call (`WEB_TONE_TIMEOUT_SECONDS`), none started with
  under 1 s left (`WEB_TONE_MIN_SECONDS`);
- summaries: 5 calls per request (`WEB_LLM_CALLS`), 8 s timeout (`WEB_LLM_TIMEOUT_SECONDS`). This timeout is
  longer than the 5-second budget, so switching LLM summaries on can make a request slower.

The CLI and local runs use the config caps instead: `tone.llm_max_calls_per_run` (100) and
`llm.max_llm_calls_per_run` (60).

**Set a monthly spending limit on the OpenAI project** whose key is on Railway (OpenAI dashboard → Limits), for
example a few dollars. The code caps calls per request, not the number of requests.

The summary's own `llm_tone` is never used as the tone. The fund-aware label (a separate, evaluated call) is
the primary tone; FinBERT is its fallback.

## Configuration (`config.yaml`; every value PROVISIONAL)

| Key | Value | Reason |
|---|---|---|
| `briefing_periods` | week 7/14/3, month 30/60/10 (period/lookback/half-life days) | Applied over `window` and `tone.recency_half_life_days`; see "Briefing period" |
| `default_briefing_period` | week | The original weekly briefing |
| `window.lookback_days` | 14 | Two full periods, so a change can be computed (overridden by the preset) |
| `window.period_days` | 7 | One period equals one week (overridden by the preset) |
| `window.max_items` | 300 | Bounds runtime and LLM spend |
| `dedupe.title_similarity` | 0.90 | Titles this similar are the same story |
| `truncation.summary_chars` | 600 | Keep only the feed's short summary; never full articles |
| `truncation.reddit_selftext_chars` | 500 | Long posts: the opening is enough |
| `truncation.finbert_max_tokens` | 512 | BERT's maximum input |
| `tone.method` | llm_fund_aware | Evaluated 85% vs 53% for FinBERT on our labeled headlines; `finbert` switches it off |
| `tone.llm_model` | gpt-4.1-mini | The evaluated model; changing it needs a new evaluation |
| `tone.llm_max_calls_per_run` | 100 | Spending cap; uncached pairs beyond it use FinBERT |
| `tone.llm_values` | +1 / 0 / −1 | A label is a direction, not a strength |
| `tone.display` | strong 0.5, slight 0.15 | Words for a per-fund tone value (symmetric bands) |
| `ambiguous_stock_names` | Dow | Stock names that usually mean something else ("the Dow" index); other stock names match case-sensitively |
| `tone.model` | ProsusAI/finbert | Fallback: local, free, finance-tuned |
| `tone.source_weights` | outlet, analyst, filing 1.0; forum 0.4 | Anonymous forums are easy to brigade |
| `tone.recency_half_life_days` | 3 | "What changed this week" favors recent news (overridden by the preset) |
| `tone.min_items_for_aggregate` | 3 | Fewer items give "not enough coverage", not a number |
| `alerts.tone_change_threshold` | 0.5 | With ±1 labels and ≥ 3 headlines one neutral→negative flip moves a score by 0.33; 0.5 needs about half the headlines to turn |
| `alerts.min_exposure` | 0.05 | Only alert when more than 5% of the portfolio is affected |
| `diversity.max_share_one_type` | 0.60 | More than 60% from one source type is concentrated |
| `diversity.one_sided_tone` | 0.50 | A one-sided tone from one source type |
| `diversity.min_items` | 3 | Do not judge diversity on fewer items |
| `diversity.curated_sources` | Fed, SEC, BLS, ECB releases; SEC EDGAR | The only sources ever suggested; verify URLs before the demo |
| `cadence.rules` | horizon ≥ 0.25y weekly, otherwise daily | Long horizons do not need daily news |
| `cadence.alerts_between_briefings` | true | Material alerts are not held back |
| `llm.enabled` | true (tests: false) | Global switch |
| `llm.model_env` / `model_default` | OPENAI_MODEL / gpt-4.1-mini | Same model setting as `chat.py` |
| `llm.temperature` | 0 | Most repeatable output |
| `llm.max_output_tokens` | 300 | Caps cost per call |
| `llm.max_llm_calls_per_run` | 60 | Spending cap; the rest get templates |
| `llm.prompt_version` | brief-v1 | Part of the cache key; bump it when the prompt changes |
| `llm.summary_max_chars` / `summary_max_sentences` | 400 / 3 | Longer outputs are rejected |
| `forbidden_patterns` | you/investors should buy·sell·hold, buy/sell now, buy the dip, strong buy/sell, buy/sell/hold rating, upgraded to buy, we recommend, price target, will rise/fall, going to rise/fall, may outperform, guaranteed, … | Targets advice and forecasts, not market words: "Investors sell bonds", "sell-off" and "buyback" pass |
| `ticker_stopwords` | SHY, TIP, VT, BIL, VB, AGG | Tickers that are ordinary words; matched only as `$CASHTAG` |
| `instrument_keywords` | per catalog ticker | Fund themes, indices, sectors. ALL-CAPS keywords are case-sensitive ("TIPS") |
| `live_sources` | Fed, SEC, BLS RSS; r/investing, r/Bogleheads, r/ETFs; EDGAR off; Tiingo news off | Demo sources; users choose their own |
| `live_sources.rss_per_ticker` | Yahoo Finance, Nasdaq, Google News per-ticker feeds; headline, link and date only | Free headline feeds per fund; official releases rarely name funds |
| `live_sources.user_agent` / `request_pause_seconds` | EgoQuant-eval/0.1 / 0.5 s | Identify ourselves; be polite to feed hosts |
| `live_sources.max_live_tickers` | 15 | Bounds the briefing; holdings first by weight, then the interest areas round-robin (one per area, then a second, …; funds before single stocks, catalog order), so every area gets a slot. Left-out tickers are listed in `metadata.tickers_left_out_by_cap` |
| `eval.max_rows_per_ticker` | 20 | One popular fund must not dominate the labeling set |
| `exchange_tag_prefixes` | NASDAQ, NYSE, NYSEARCA, NYSE ARCA, NYSE AMERICAN, CBOE, BATS, OTCMKTS, OTC | "(NASDAQ:SHY)" means ticker SHY only; the exchange name is never a keyword (bare "Nasdaq" is not QQQ) |
| `non_news_patterns` | quote, chart, holdings-history and options-chain pages, ticker lists, listing notices, bare "Fund ETF (TICKER)" pages, titles ending in "..." | Not news: labelers and models would all say neutral, inflating accuracy. Dropped in `prepare` (counted as `n_non_news`) |
| `eval.sample_size` / `random_seed` / `llm_neutral_band` | 120 / 42 / 0.10 | Reproducible labeling sample |

## Sources and licences

- **FinBERT** — [ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert) on Hugging Face (Araci, 2019).
  The model page lists **no licence**; the original FinBERT code (ProsusAI/finBERT on GitHub) is Apache-2.0.
  Confirm the terms before commercial use.
- **feedparser** — BSD-licensed RSS/Atom parser.
- **Reddit** — official Data API with OAuth credentials. Follow Reddit's Data API Terms: a descriptive
  User-Agent, rate limits, and no commercial use without an agreement.
- **SEC EDGAR** — fair-access rules: a descriptive User-Agent with contact details (`SEC_USER_AGENT`) and at
  most 10 requests per second. ETF filings are made by the issuing trust, which often covers many funds.
- **Loughran–McDonald Master Dictionary** (Loughran & McDonald, 2011, *Journal of Finance*), Notre Dame
  Software Repository for Accounting and Finance. It is free for academic research; other uses need permission.
  It is **not included** here. Download it and pass `--lm-dictionary` or set `LM_DICTIONARY_PATH`.
- **OpenAI** — summaries only, through the Responses API with structured output and `store=False`. Only
  public article text is sent. Subject to OpenAI's usage policies.
- **Fixture items** — invented for the demo, under invented source names and `example.com`/`example.org`
  links (reserved example domains). They are not real news and attribute nothing to real outlets, companies or people.

## Known limitations

- Tone measures the wording of headlines, not the future. It is not a forecast.
- The monthly view needs history: live, it only compares once the local archive holds about two months of
  headlines. The archive is per machine (not shared between team members or the deployed app).
- The fund-aware LLM is wrong about 15% of the time on our labeled set (and the set is small: 120 headlines from
  one 14-day window, labeled by our own team). It costs a little per uncached pair and sends headlines (never
  investor data) to OpenAI. In the evaluation, a headline about several funds was judged in one call for all of
  them; in production each fund is asked separately (most labeled rows had one fund).
- With ±1 labels a per-fund score moves in large steps (one headline in a small window can swing it by 0.3–0.7);
  which is why `alerts.tone_change_threshold` is 0.5; see `eval/alert_thresholds.py` before changing it.
- FinBERT (the fallback) scores the wording of a headline, not its effect on the specific fund the investor holds:
  "Treasury yields jump" reads the same for a long-bond fund (bad news) and for T-bills (barely matters).
  Target-aware sentiment is future work; the human-label evaluation (labels judged for the matched fund)
  measures how much this costs.
- With `tone.neutral_gating` (default on), an item whose most likely FinBERT class is neutral has tone 0,
  matching the label the evaluation compares against; the raw probabilities are always stored.
- Headline-level scoring misses context and sarcasm. FinBERT also reads some neutral phrasing as negative:
  in the sample data, "REITs steady as rent collections hold up" scores −0.78. That is why we evaluate it.
- Forums can be manipulated, so they are down-weighted and trigger source-diversity suggestions.
- RSS summaries are short, so the LLM may have little to work with. It is told to say less.
- The LLM can be wrong. Its output is schema-validated, length-checked and advice-filtered, with a template fallback.
- Keyword matching can produce false positives (bare "gold" is excluded for this reason) and misses
  instruments that are only implied.
- The advice filter targets advice and forecast phrasing ("you should buy", "buy now", "strong buy", "sell
  rating", "price target", "will rise/fall", "guaranteed"). Factual market language ("Investors sell bonds",
  "sell-off", "buyback") passes. Unusual advice wording can slip past a pattern list, so the LLM is also told
  not to advise, and every rendered line is checked again.

## Evaluation (results vs a baseline, for the pitch)

1. `python eval/collect_headlines.py` (network): fetches the configured RSS feeds, the per-ticker feeds for all
   US-listed catalog funds, and Reddit if credentials are set. It strips Google News's " - Publisher" suffix,
   keeps headlines that match catalog instruments, removes duplicates, and samples 120 with seed 42 (at most
   15 per fund). It writes `eval/headlines_to_label.csv` (gitignored; third-party text) and scores nothing.
   `python eval/make_labeling_sheet.py` then builds `eval/labeling_sheet.csv` (blocks A–E); see
   `eval/LABELING_GUIDE.md`.
2. Two team members label each headline independently (`label_1`, `label_2`: positive, negative or neutral)
   and agree a `final_label`.
3. `python eval/merge_labels.py` merges the returned files (agreement rate, Cohen's kappa per pair), creates
   `eval/labeling/disagreements.xlsx` for the disagreements, and, once its `final` column is filled, writes
   `eval/headlines_labeled.csv` (`agreed` / `adjudicated` rows). That file is gitignored: labels are never
   committed. The 10 example rows now live in `tests/briefing/data/headlines_labeled_example.csv` (tests only).
4. Run `python eval/run_eval.py --lm-dictionary <path>`. It reports accuracy, macro-F1 and how many headlines
   each method scored, for the LM baseline and FinBERT (both free), in `eval/results.md` and `.csv`.
   `--with-llm` also scores LLM tone. It is opt-in: it prints the number of calls first and stops above the
   cap unless `--yes` is given. Results are cached.

Do **not** evaluate on Financial PhraseBank: FinBERT was fine-tuned on it, so that is not a fair test.

## Security self-assessment

- **Prompt injection:** article text sits inside `<article>` tags, and the system prompt says it is data and
  any instructions in it must be ignored. Whatever the model returns must still pass the schema, the length
  and sentence limits and the advice filter. The sample data includes an injection attempt, and a test shows
  that an obeying model still cannot get advice through.
- **No investor data to the LLM:** the request type has no field for it, and a test inspects the exact payload.
- **Advice filter** on LLM output, on templates (headlines) and on every rendered line of the report section.
- **Output escaping:** all external text is HTML-escaped, and only http(s) links are rendered.
- **Keys** come from environment variables only. `.env` is gitignored and `.env.example` lists names only.
- **Offline fixture mode** for demos and tests: no network, no downloads, no OpenAI.
- **The briefing cannot change the portfolio:** it runs after Agent A and the Risk Engine, reads copies, and
  its only action is offering a re-check that the user must confirm.
