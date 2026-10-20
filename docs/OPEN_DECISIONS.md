# Open decisions for Savas

Seven items need a human choice. Each has the options and my recommendation. None blocks the submission; the default
(what runs today) is marked **now**. Owner of the questionnaire, preferences and the IBKR parser: Bartu.

## 1. B3_needed wording (questionnaire, our own item, not Grable-Lytton)
**Options:** (a) keep "Of that amount, how much must you have back by your goal date?" **(now)**; (b) "What is the least you could accept having by your goal date?"; (c) ask the loss directly ("What is the biggest fall you could accept?").
**Recommendation: (b).** Same number, same scoring (loss capacity = 1 - needed / invested), but (a) reads as a promise that the money comes back; (c) changes the scoring input and needs a new test pass.

## 2. P1 crypto: show the 2% floor and the 5% cap
**Options:** (a) say nothing **(now)**; (b) a line under "Bitcoin and Ether" and "Other cryptocurrencies" read from config at runtime: "If you pick this, at least 2% of the portfolio goes here, and at most 5%"; (c) drop the floor (decision 20).
**Recommendation: (b).** Today choosing crypto silently forces 2% (and C5 is capped at 5%); an informed choice costs one sentence. About 20 lines (the preference form adds a note, the interests page prints it) once you say yes.

## 3. K1 to K3 "Refuse to answer"
**Options:** (a) keep all five options **(now)**; (b) hide "Refuse to answer" in the UI and keep "Do not know" (decision 7); (c) delete it from the question file.
**Recommendation: (b).** The three questions stay verbatim (Lusardi-Mitchell); "Refuse" and "Do not know" both score as wrong (`scoring.py`), so showing both only adds a button. Hiding it is a UI change, not a scoring change.

## 4. Two "crash" numbers: illustrative_crash_loss vs the portfolio's stress scenario
**Options:** (a) keep both **(now)**; (b) reword the Profile page from an estimate to the limit: "Your category allows a loss of up to 20% in a severe market fall (hypothetical); your portfolio will show its own figure"; (c) run Agent A first and show its figure on the Profile page.
**Recommendation: (b).** The Profile figure is not an estimate, it is the category's loss limit (C1 5%, C2 10%, C3 20%, C4 35%, C5 45%, identical to `max_stress_loss`), while the portfolio page shows what THIS portfolio would lose (demo: C2 4.4%, C3 15.6%, C5 27.0%). Same word "crash", different meaning, so a judge sees "a C3 portfolio could fall to 40,000" and then "worst case 15.6%". (c) needs the portfolio before the profile page exists.

## 5. Importing trades from brokers other than IBKR
**Options:** (a) IBKR only: Flex report upload and Flex token, read-only **(now, TEAM_BRIEF "other broker presets: dropped")**; (b) one generic CSV template (date, side, ticker, quantity, price) with a column picker; (c) named presets for Schwab, Fidelity, Robinhood.
**Recommendation: (a) for the submission, (b) on the roadmap.** Every format needs its own fuzz, privacy review and real samples we cannot get safely before the deadline; the IBKR parser now has 300+ fuzz cases (XML, CSV, token) and two fixed bugs from last night's run. Say "other brokers: roadmap" if asked.

## 6. Answers in Chinese (the trade explanation is English only; the advisor chat answers in the language of the question)
**Options:** (a) English only (for the trade explanation); a Chinese question gets an English answer or the standard text **(now)**; (b) Chinese answers for the trade explanation first (needs a Chinese template, Chinese guard words, a number check for 万, 百分之 and full-width digits, a Chinese red-team and a native reviewer, about 1.5 to 2 days); (c) Chinese for the whole app (a separate project).
**Recommendation: (a) for the submission,** with the English-only limit stated in the docs and the demo; (b) only with a reviewer for the compliance wording. A Chinese reply that the English guards cannot read is worse than an English one.

---

## 7. News worker: stop rating for some hours after "insufficient_quota"
**Found 20 Oct on the deployed backend** (`GET /data/status`, news worker): `pairs_rated` 0, `pairs_failed` 1500, `last_run_seconds` 3037. The OpenAI credit is used up, so every
fund-aware rating fails. The worker (Agent D, `briefing/worker.py` and `briefing/rating.py`, not my files) still tries its whole run of `labels_per_run` = 1500 pairs, each pair three attempts
with sleeps of 0.5, 1.0 and 1.5 s between them, 10 in parallel: 3037 s is 150 batches of 10 pairs at about 20 s each, i.e. 50 minutes of one thread per run, and the next run starts 15 minutes after it ends.
Every failed pair is also counted in `labels_today` (`labels_per_day` = 3000), so after two runs (about two hours) the day's budget is spent on failures; when credit is back the same day, nothing is rated until the next UTC day.
The same retry loop (`rate_pairs`) serves the investor's own briefing job (`/briefing/start`, `max_seconds` 150), which then spends up to 150 s on answers that cannot come.
**Options:** (a) leave it **(now)**; (b) a circuit breaker: when an answer is a 429 with the error type `insufficient_quota` (the test `chat.py` already uses), abandon the run at once, remember "no credit" for N hours (suggested 6, in config, PROVISIONAL), skip rating but keep fetching and archiving the headlines, do not count the failed pairs against `labels_per_day`, and show `quota_blocked_until` in `/data/status`; (c) switch the worker off with `EGOQUANT_NEWS_WORKER=off` until the credit is back.
**Recommendation: (c) today** (no code, Railway variable), **(b) as the proper fix** (about 30 lines in `rating.py` and `worker.py`, with a test per rule), owned by whoever owns Agent D. Needs Savas's yes before it is sent to Aslan's agent through the coordination file.

---

## 8. Live red-team of the advisor (done 20 Oct, with Savas's go)
Run once on 20 Oct (a few cents of OpenAI credit): `python eval/chat_redteam.py` (24 older cases and 18 `trade_*` cases for the trade tool) and `python eval/trade_explain_redteam.py`. The first run of `advisor-v10` passed 34 of 42 and found that the live model filled both size fields of the `check_trade` tool on every call, so no trade check completed (the 8 failures were safe). The tool now takes one size (`advisor-v11`); the re-run passed 41 of 42 (17 of the 18 trade cases; the one miss is a false positive of the keyword check) and the trade explanation `trade-explain-v4` passed 51 of 51. Raw results: `eval/results/` (`chat_redteam_20261020_*.jsonl`, `trade_explain_redteam_20261020_*.jsonl`). `python eval/chat_redteam.py --list` shows every case and what it expects without calling the model. Any later prompt change needs a new live run.

## 9. The Risk Game can be played again (open, bounded, stated)
The same person can replay the game and get a different category (found in the 13-stranger test: questionnaire alone C3; a timid game C2, a
bold game C4). The effect is bounded at one step each way from the questionnaire's own category and never past the capacity cap, and the
result is client-reported like every questionnaire answer (`docs/SECURITY.md` T19). Options if it matters: keep only the first result per
profile version, or show "your result is saved" in the app; neither is built.

---

## Resolved since the first version of this list
- Capacity is averaged (an irregular income no longer caps everybody at C3) with two answers that cap on their own: money that must come
  back (B3) and a short horizon, under a year level 2, under three years level 3 (`capacity.answer_caps`; decided 20 Oct, approved by Onur after
  the 13-stranger test found that big savings averaged a one-year horizon up to C4). `egoquant/risk_profile/README.md`, `docs/judges/03_METHODS_AND_MODELS.md` section 2.
- One vocabulary for a trade verdict everywhere an investor reads it: "within your limits", "within your limits, with warnings", "outside your
  limits" (the codes advisable / allowed_with_warnings / not_advisable stay the API values). Decided by Onur 20 Oct: the word "advisable" is
  advice vocabulary even when a rule produces it.
- `egoquant_routes._ibkr_prices` falls back to the committed price snapshot (Onur approved; in oct19), so Agent B sees the market fall at the time of a sale even when the live feed is down.
- `chat.py`: one plain sentence for every case in which the AI cannot answer (no key, no credit, rejected key, provider error); the operator wording is gone (Onur; in oct19).

---

## Two answers to have ready: portfolio method v2 (plain language)

Method v2 is behind a flag and is the default only after Savas accepts the before/after table (`python eval/portfolio_v2_compare.py`, checks of
`docs/PORTFOLIO_V2.md` section 5). The same two answers are in the demo script ("What to say if asked", 6 and 7). Say the first one in the
order it is written; each step is code, none is chosen by an AI.

### How is the portfolio built?
1. **What you may hold.** Your risk category, your knowledge answers and your trading platform decide which products are allowed at all; every product you are eligible for is considered. Your interests and the industries you avoid count only among those.
2. **One fund per look-alike group.** Many funds are near-copies (their prices move almost in step). We group them and keep one per group, the cheapest, so the optimiser is not fooled into thinking it is diversified.
3. **No forecasts from the past.** The starting expectation for each product is a neutral market-style baseline for your category (the Black-Litterman method), **minus the fund's fee**. What you like or dislike moves it by half a percentage point a year at low confidence, never by more.
4. **Many candidates, established methods.** At about twenty stock shares we build candidates by risk parity (equal risk per holding), hierarchical risk parity, maximum diversification, Black-Litterman mean-variance and its resampled version; the plainer styles of the older method stay in the family, so the best chance cannot be lower than before. All obey the limits: no fund above 30% for categories C3 to C5 (a single stock above 10%), no group of near-identical stock, crypto or commodity funds above a quarter of the portfolio (dropped, and said so in the report, only if it would make the portfolio impossible), volatility and stress-loss caps.
5. **Shaken estimates, fixed seed.** The resampled candidates average several slightly perturbed estimates, with a seed derived from your inputs: the same person always gets the same portfolio.
6. **The choice.** With a goal: among portfolios whose simulated chance of reaching it is within 1 to 3 points of the best, the one with the smallest drawdown in a bad stretch (the fall exceeded in the worst 10% of simulated paths); falls within a point of each other are a tie and the more spread-out portfolio wins. Without a goal: the best balance of assumed return and risk among portfolios spread over enough funds.
7. **The last word is not the optimiser's.** The independent Risk Engine re-checks every limit; the behavioural agent can only tighten; the AI only explains.

### Why not just buy the cheapest fund?
- **A fee is not the biggest cost.** A world-stock fund at 0.06% a year fell by half between 2008 and 2009 (committed prices); a C3 investor's severe-fall limit is 20%, so one fund of that kind breaks the limit by itself.
- **One fund is one bet.** For categories C3 to C5 no fund may hold more than 30%; only the most cautious category (C1) may hold a single fund.
- **We do use cost, where it is the right tiebreaker:** among near-identical funds we keep the cheapest, and every expected return is counted after its fee.
- **What you asked for matters:** goal, interests and exclusions shape the mix. If the cheap broad fund fits, it will be a large part of the portfolio.
- Honest limit: a prototype with placeholder limits (marked PROVISIONAL); nothing here is a forecast.

