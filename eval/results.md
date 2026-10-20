# Tone evaluation: methods vs hand-labeled headlines

Generated 2026-10-18 12:40 UTC by `eval/build_results.py`. 120 headlines labeled by our team (90 agreed by both labelers, 30 adjudicated). Labels judge each headline for an investor who holds the matched fund.

## Results

| Method | All: scored | All: accuracy | All: macro-F1 | Agreed only: scored | Agreed: accuracy | Agreed: macro-F1 |
|---|---|---|---|---|---|---|
| Always "neutral" (naive baseline) | 120/120 | 0.375 | 0.182 | 90/90 | 0.400 | 0.190 |
| Loughran-McDonald word counting | 120/120 | 0.375 | 0.320 | 90/90 | 0.411 | 0.357 |
| FinBERT (plain) | 120/120 | 0.533 | 0.511 | 90/90 | 0.622 | 0.601 |
| FinBERT + cross-validated neutral margin (held-out) | 120/120 | 0.551 | 0.528 | 90/90 | 0.633 | 0.627 |
| LLM tone (summarizer llm_tone, existing --with-llm) | 120/120 | 0.483 | 0.395 | 90/90 | 0.533 | 0.442 |
| LLM tone, fund-aware (gpt-4.1-mini) | 120/120 | 0.850 | 0.852 | 90/90 | 0.889 | 0.890 |

- Accuracy and macro-F1 are over the headlines each method could score; 'scored' shows coverage.
- FinBERT label = the most likely of its three classes (the same rule as Agent D's neutral gating).
- LLM tone (existing) = the summarizer's llm_tone, neutral within ±0.10.
- Cross-validated margin, mean of the 5 held-out folds: accuracy 0.551, macro-F1 0.528.
- Uncertainty: with 120 headlines, an accuracy near 0.85 has a 95% interval of about ±0.064, near 0.53 about ±0.089 (normal approximation); differences of a few points are not meaningful.
- The fund-aware LLM matches the final labels more often than two individual labelers matched each other (75%): the final labels are a consensus, more reliable than one person's label, and the model was asked the same fund-aware question the labelers answered. FinBERT judges wording only.

## Reference: human agreement

Two labelers agreed on 90/120 headlines (75%) before adjudication. Cohen's kappa per labeler pair:

| Pair (shared block) | Headlines | Kappa |
|---|---|---|
| person1 + person2 (B) | 30 | 0.648 |
| person1 + person4 (A) | 30 | 0.702 |
| person2 + person3 (C) | 30 | 0.674 |
| person3 + person4 (D) | 30 | 0.442 |

## FinBERT neutral margin (5-fold cross-validation)

Predict positive/negative instead of neutral when max(P(pos), P(neg)) >= P(neutral) − m, with m from {0.0, 0.1, 0.2, 0.3, 0.4, 0.5} chosen on 4 folds (best macro-F1) and scored on the held-out fold (stratified by label, seed 42). The table above reports the mean of the held-out folds for 'All' and the pooled held-out predictions for 'Agreed only'.

| Fold | m chosen | Held-out headlines | Accuracy | Macro-F1 |
|---|---|---|---|---|
| 1 | 0.5 | 25 | 0.600 | 0.610 |
| 2 | 0.5 | 25 | 0.480 | 0.456 |
| 3 | 0.5 | 24 | 0.500 | 0.480 |
| 4 | 0.4 | 23 | 0.609 | 0.566 |
| 5 | 0.5 | 23 | 0.565 | 0.529 |

## Confusion table: FinBERT (rows = human label, columns = FinBERT)

| Human \ Model | positive | negative | neutral | unscored |
|---|---|---|---|---|
| positive | 11 | 4 | 27 | 0 |
| negative | 5 | 16 | 12 | 0 |
| neutral | 5 | 3 | 37 | 0 |

| Class | Precision | Recall |
|---|---|---|
| positive | 0.524 | 0.262 |
| negative | 0.696 | 0.485 |
| neutral | 0.487 | 0.822 |

## Confusion table: LLM tone, fund-aware (rows = human label, columns = LLM tone, fund-aware)

| Human \ Model | positive | negative | neutral | unscored |
|---|---|---|---|---|
| positive | 37 | 0 | 5 | 0 |
| negative | 1 | 28 | 4 | 0 |
| neutral | 4 | 4 | 37 | 0 |

| Class | Precision | Recall |
|---|---|---|
| positive | 0.881 | 0.881 |
| negative | 0.875 | 0.848 |
| neutral | 0.804 | 0.822 |

## Examples

FinBERT wrong, fund-aware LLM right:

| Headline | Fund | Human | FinBERT | Fund-aware LLM |
|---|---|---|---|---|
| Bitcoin Behemoths Strategy and IBIT Amass 1.65 Million BTC Worth $140B | IBIT - bitcoin fund (ETF); BTCUSDT - bitcoin | positive | neutral | positive |
| Silver Falls to $60. Is Now the Time to Load Up on the Precious Metal? | SLV - silver | negative | neutral | negative |
| Bitcoin Returns to ETF Holders’ Breakeven Price, and $729 Million Heads for the Exit | BTCUSDT - bitcoin | negative | positive | negative |
| SCHD: Buy Signal Is Finally Printing (Upgrade) (NYSEARCA:SCHD) | SCHD - US dividend-paying stocks | positive | neutral | positive |
| Vanguard (VB) vs. State Street (SPSM) in a Small-Cap ETF Showdown | VB - US small-company stocks | neutral | positive | neutral |

Fund-aware LLM wrong:

| Headline | Fund | Human | FinBERT | Fund-aware LLM |
|---|---|---|---|---|
| Three international dividend ETFs outperformed the S&P 500 and SCHD with higher yields over the past year. | VTI - whole US stock market; SCHD - US dividend-paying stocks | negative | positive | neutral |
| Silver ETFs fall up to 5% as high oil prices weigh on sentiment. What should investors do? | SLV - silver; PDBC - broad commodities (oil, metals, crops) | neutral | negative | negative |
| James Thorne Calls Fed's Hike Signal a 'Misdiagnosis' - iShares 7-10 Year Treasury Bond ETF (NASDAQ:IEF) | IEF - 7-10 year US Treasury bonds | positive | negative | neutral |

## Runs and cost

- Fund-aware LLM (eval/run_fund_aware_llm.py, prompt hash ebb752b54750, fixed and committed in 1adb563 before the run): 1 run, 120 calls, 23,997 input / 240 output tokens, about $0.010 at gpt-4.1-mini list price; 0 unscored answers.
- LLM tone, existing (eval/run_eval.py --with-llm --yes): 1 run, 120 calls, 42,090 input / 7,273 output tokens, about $0.029 at list price.
- Total OpenAI spend for this evaluation: 240 calls, about $0.04. FinBERT and Loughran-McDonald: local, free.

## Limitations

- 120 headlines from one 14-day window of free news feeds; the mix is dominated by crypto, tech and dividend funds. Results may not generalise to other periods or funds.
- Labels were made by our own team (4 people, every headline labeled twice), not by independent experts.
- The 30 disagreements were resolved by one adjudicator.
- The fund-aware prompt (`eval/llm_fund_aware_prompt.txt`) was fixed and committed before running, and was run once; it was not tuned on these headlines. The neutral margin was chosen by cross-validation, never on the scored fold.
- Both LLM methods send headlines (and, for the fund-aware method, the fund names) to OpenAI; never investor data. They cost money per call. FinBERT and the dictionary run locally for free.
- Agent D's official tone remains FinBERT; the LLM methods here are an evaluation, not a product change.
