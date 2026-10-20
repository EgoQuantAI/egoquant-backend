# Agent D (Market Briefing): what it measures and where it falls short

In the app this is the **Market briefing** on the News tab ("Agent D" is only its name inside the code). It reads free news feeds (headline, link and date only, no article bodies). It links each headline to the
funds an investor holds and rates it for holders of each fund. Then it reports how the news tone moved this week
or month compared with the one before. It is read-only: it never changes a portfolio or forecasts prices, and it
sends no investor data to the AI model (only the headline and the fund's ticker, name and description). Every
number below lives in `egoquant/briefing/config.yaml` and is PROVISIONAL.

## 1. How good the ratings are

| What | Result | How it was measured |
|---|---|---|
| Tone per headline and fund (good, bad or neutral for holders) | 85% agreement with our human labels (FinBERT: 53%) | 120 hand-labelled headlines; the prompt `prompts/fund_aware_tone.txt` is hash-pinned (`ebb752b5…`, checked by a test) |
| Relevance: is an attached headline about the fund or what it holds? | 28 of 30 on-topic | 30 random attached (headline, fund) pairs from three local portfolios (C3, C5 default, 25 holdings), week and month, 19 Oct 2026 |

Thirty is a small sample: the 95% interval for 28/30 is roughly 79% to 98%. The sample also had one borderline pair
we counted as on-topic: an article about international dividend ETFs attached to VIG (a US dividend fund) through
the "dividend stocks" theme.

### The two misses, and why they are hard

1. **"Stablecoin Giant Circle Expands Into 40 Emerging Markets Amid Global Adoption"** was attached to VXUS
   through the *Emerging markets* theme. The phrase "emerging markets" is right; its sense is not. Here it names
   where a payments company is expanding, not emerging-market shares. A keyword or theme rule cannot tell the two
   apart without understanding the sentence.
2. **"Depleted Global Stocks Set Higher Floor Under Oil Prices"** was attached to VT (global shares). "Stocks"
   means oil inventories here. The words are the ones a genuine global-equities headline would use.

Both are word-sense problems. The fix would be a relevance check per (headline, fund) pair: a second AI question,
or a change to the pinned prompt. Either would need its own labelled evaluation and would roughly double the AI
cost. We kept the pinned prompt unchanged so the 85% result stays valid, and we stopped tuning here.

### Rules added while measuring (each has a regression test)

- Non-US government bills (Pakistan, Nigeria, Iceland RIKV, Ghana, Kenya, India…) never match US T-bill or
  Treasury funds. Generic bill or auction words need a US signal and no foreign one (country, demonym,
  currency, foreign central bank, non-US listing tag such as `LSE:`, "EM").
- A headline about **another named fund** ("…ETF (TLTE)", "NASDAQ:PFM", "VOO vs. IVV") or about **one other
  product** ("The S&P 500 ETF You Know…") does not count for our fund through topic words. Our fund's own name
  or ticker still counts.
- "Wall Street" alone is not US-stock news ("Wall Street analysts…"). "Powell" alone is not a Fed signal
  ("Powell's downtown revitalization…"). Crypto headlines are never stock-market news ("BNB Chain claims 42.6% of
  tokenized stock market"). Options "credit spreads" are not bond news.

The strict US-signal rule also drops some genuine US headlines that carry no US word. For example, "Funding
Markets Diverge as T-Bills Cheapen" no longer reaches BIL directly. Theme news (Fed, T-bill and money-market
searches) makes up for most of this.

## 2. How a score, a confidence level and an alert are made

- Each rated headline counts **+1 / 0 / −1** for holders of the fund.
- Weight per headline: **w = source tier × recency × relation**.
  - Source tier: established 1.0, opinion & analysis 0.7, not yet reviewed 0.4. Automated pages are not used.
  - Recency: the weight halves every 3 days in the weekly view and every 10 days in the monthly view.
  - Relation: 1.0 when the headline is about the fund; **0.6** when it is about what the fund holds (for
    example Fed news for a T-bill fund).
- **Damped score = Σ(w·s) / (Σw + 1.5)**. With little news the score is pulled toward 0 instead of being hidden.
  One fresh negative headline from an established source gives about −0.35, not −1.
- **Confidence** follows the evidence Σw: high from 4.0, medium from 1.5, low below 1.5.
- **Final states only:** a fund is rated (with its confidence), *quiet* ("no news about X or what it holds"), or
  *not rated* (news found, no rating available). It is never left at "rating in progress".
- **Comparison:** this period against the previous one, only when **both** have medium or high confidence.
  Otherwise the briefing says so ("VT: too little news last week to compare").
- **Re-check offer:** shown for a holding of more than 5% of the portfolio when its news tone **fell** by 0.5 or
  more (Δ ≤ −0.5, medium or high confidence in both periods), or when its price closed 10% or more below its
  21-day high (Agent C's drawdown alert). A tone rise of the same size is reported without the offer. Interest
  areas never trigger anything. "What changed" names holdings only.

## 3. Caps and costs

| Cap | Value |
|---|---|
| Tickers with their own feeds per briefing | 30 (holdings first, larger before sub-2% ones, then interest areas) |
| Exposure headlines per theme | 15 a week, 40 a month (newest first, each headline counted once) |
| Pairs rated inside one briefing job | 2500; the rest go to the background worker |
| Background worker | every 15 min, up to 1500 pairs a run and 3000 a day (about $0.30 a day) |
| More than 6 holdings | grouped by exposure theme; holdings under 2% collapsed into "Small positions" |

Measured on 19 Oct 2026, live news, gpt-4.1-mini. "Empty cache" means nothing had been rated before. Costs are
estimated from tokens ($0.0248 for 317 pairs, about $0.00008 a pair).

| Portfolio | Pairs (empty cache) | Time to done | Cost | Week confidence (high / medium / low / quiet) | Headlines per holding, week / month |
|---|---|---|---|---|---|
| C3 (5 holdings) | 1148, all rated | 81 s | ≈ $0.09 | 100 / 0 / 0 / 0% | 52 / 103 |
| C5 default (8 holdings) | 855, all rated | 60 s | ≈ $0.07 | 88 / 12 / 0 / 0% | 49 / 101 |
| 25 holdings, broad interests | 2679 found, 2500 rated: **every holding's pairs rated**; 179 interest-area pairs left to the worker | 176 s | ≈ $0.20 | 88 / 12 / 0 / 0% | 34 / 78 |

With news already rated (the normal case once the worker runs), a briefing takes 22–40 s, mostly reading feeds,
and costs under $0.02.

## 4. Other limits

- Headlines only: a headline can be misleading where the article is not. We never fetch article bodies.
- Free feeds in English; coverage of non-US funds is thinner than of US ones.
- Single stocks have no exposure themes; they get news only when they are named.
- Tone describes the news, not the market. It is not a forecast and is never used to change a portfolio.
- If a cache file is damaged it is read as empty and rebuilt from the next fetch. The briefing still works, but
  older archived headlines for the monthly view are lost.

## 5. What is cached

Everything lives under `EGOQUANT_CACHE_DIR/briefing` (`egoquant/briefing/loader.py`; never in the repository):

| File | What it holds | Shared between investors? |
|---|---|---|
| `fund_tone_cache.json` | one AI rating per (headline id, ticker, prompt hash, model) | yes: a rating never says whose portfolio a fund is in, and a cached pair is never sent again |
| `llm_cache.json` | AI summaries of headlines | yes (headline text only) |
| `tone_cache.json` | FinBERT scores (local Macs only; FinBERT is never run on the server) | yes |
| the news archive | headline, link, date, source and matched tickers of every fetched item, never article bodies | yes |

No investor data, weight or account id is in any cache file. **Railway's disk is temporary:** after a redeploy the caches start
empty, so the first briefings re-rate their headlines (about 60 to 180 s and $0.07 to $0.20 for a portfolio, table above);
a Railway volume pointed at `EGOQUANT_CACHE_DIR` would keep them (optional, `egoquant/briefing/README.md`).

## 6. Without OpenAI (no key, AI switched off, or no credit left)

- **Advice is never affected:** the briefing never sets a score, weight, limit or approval.
- **Tone:** cached ratings are still used (they cost nothing and send nothing). Headlines not yet rated are shown as
  **"not rated"**, with the plain reason in the briefing: "AI rating is switched off on this server" (no key or switched off)
  or "the AI rating service did not answer for these headlines" (an API error, for example credit exhausted). FinBERT
  is not a fallback on the server (`egoquant/briefing/jobs.py`, `_finbert_inner`).
- **Summaries:** every failure (no key, API error, timeout, invalid answer, advice-filter hit) falls back to a fixed
  template built from the headline (`egoquant/briefing/summarize.py`).
- **The job still finishes**, so the report's news counts as final and the plan can still be confirmed and sealed
  (`egoquant/briefing/final.py`, `news_is_final`): a missing AI never blocks the seal.
- Seen live on 18 to 19 Oct: when the credit ran out, new headlines showed "not rated" until the credit was topped up;
  afterwards 453 of 453 pairs of a test portfolio were rated.

