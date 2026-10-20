# Risk questionnaire module (`egoquant/risk_profile`)

Formal, fixed-order, multiple-choice questionnaire → deterministic `RiskProfile` (C1–C5) →
numeric limits for Agent A, Agent C and the Risk Engine. No LLM is involved in scoring.

```
Answers ──scoring.build_risk_profile──> RiskProfile
   RiskProfile ──adapter.to_agent_c_limits──────────> Agent C  {max_r_level, blocked_product_types}
   RiskProfile + Agent C selection
               ──adapter.to_investor_context_limits─> Agent A  InvestorContext fields
   RiskProfile ──adapter.to_constraints─────────────> Risk Engine  [max_equity, max_stress_loss]
   RiskProfile ──explain.explain────────────────────> results page text
```

Agent A never sees the score: it gets the C-level (`InvestorContext.risk_class`) and numbers
(`suitability_max_equity`, `max_acceptable_loss`, `excluded_assets`, and for C1 `max_asset_weight`).
`egoquant/pipeline.py` (`run_pipeline`) wires the whole chain for one investor.
`Constraint` is the shared class in `egoquant/models.py`; this module does not define its own.

**R-levels (two layers).** Product R-levels come only from Agent C's catalog (`Instrument.r_level`).
Layer 1: Agent C drops every instrument above the investor's `max_r_level` from Agent A's universe;
held ones go to `excluded_assets`, and the adapter also excludes any selected instrument above the limit
as a backstop. Layer 2: `egoquant/risk_engine.py` re-checks every final holding's R-level, the equity
cap and the stress loss, with its own arithmetic, and rejects unknown tickers (fails closed).

| File | Contents |
|---|---|
| `questions.json` | Question bank, version `eq-rq-0.1.0`: ids, text, options, points, answer key |
| `config.yaml` | Every band, cap, cutoff and table. **All PROVISIONAL.** |
| `models.py` | `Answers`, `RiskProfile`, `AnswerAuditRecord`, plus validated config/bank models |
| `loader.py` | The only file that reads from disk |
| `scoring.py` | Pure scoring functions and `audit_records` (the caller passes the timestamp) |
| `adapter.py` | `RiskProfile` → Agent C limits, Agent A context fields, Risk Engine constraints |
| `explain.py` | Template sentences for the results page |

Tests: `pytest tests/risk_profile tests/system/test_risk_engine.py`.

```python
from datetime import datetime, timezone
from egoquant.risk_profile import load_all, build_risk_profile, to_agent_c_limits, to_investor_context_limits, to_constraints
bank, config = load_all()
profile = build_risk_profile(answers, bank, config, assessed_at=datetime.now(timezone.utc))   # caller supplies the time
interests = InvestorInterests(investor_id=..., interest_areas=..., **to_agent_c_limits(profile, config))
agent_c = MarketIntelligenceAgent(interests)
context = InvestorContext(..., risk_class=profile.c_level, **to_investor_context_limits(profile, config, agent_c.selection))
result = QuantAgent(agent_c, agent_b, AgentConfig()).run(context, as_of)
review = RiskEngine().review(result.final, assets, to_constraints(profile, config), profile.max_r_level)
```

`amount_needed` larger than `amount_invested` is rejected with an error (almost always a typing mistake).

## Sources

**Section GL: risk tolerance.** Grable & Lytton 13-item scale, taken **verbatim** (wording,
original dollar amounts, original spelling such as "Three month’s salary", and point values) from the
Rutgers Cooperative Extension *Investment Risk Tolerance Quiz*. The original URL
(`https://njaes.rutgers.edu/Money/assessment-tools/investment-risk-tolerance-quiz.pdf`) now returns 404,
so we cite the Internet Archive copy:

<https://web.archive.org/web/20241223080146/https://njaes.rutgers.edu/money/assessment-tools/investment-risk-tolerance-quiz.pdf>

All archived snapshots from 2022-07 to 2025-06 have the same content digest
(`QRBAFPNODH6TFAD6BHGV5XH2BNUUZTYM`), so the document did not change in that period. Every item and
point value in `questions.json` was checked by script against that PDF's text.

- Grable, J. E., & Lytton, R. H. (1999). Financial risk tolerance revisited: The development of a risk
  assessment instrument. *Financial Services Review*, 8, 163–181.
- Grable, J. E., & Lytton, R. H. (2003). Testing the validity of a risk assessment instrument.
  *Financial Services Review*, 12(3).
- Kuzniak, S., et al. (2015). The Grable and Lytton
  risk-tolerance scale: A 15-year retrospective. *Financial Services Review*, 24(2). n = 160,279;
  Cronbach's α = 0.77.
- **Licensing:** the FSR papers are published under CC BY-NC 4.0. Commercial use of the scale needs
  permission from the authors/rights holders. Our hackathon demo is non-commercial.
- The Rutgers PDF's intro note ("you will be contributing to a study … recorded anonymously") does not
  apply to EgoQuant and must **not** be shown in our UI. Only the 13 items are used.

**Section K1–K3: financial literacy ("Big Three").** Wording taken verbatim from:

- Lusardi, A., & Mitchell, O. S. (2011). *Financial Literacy and Planning: Implications for Retirement
  Wellbeing.* NBER Working Paper No. 17078. <https://www.nber.org/papers/w17078>
  (PDF: <https://www.nber.org/system/files/working_papers/w17078/w17078.pdf>)

Answer key: K1 "More than $102", K2 "Less than today", K3 "False". The answer options "Do not know"
and "Refuse to answer" follow the HRS survey convention and count as incorrect.

**Sections B (capacity), K4–K5 (experience), D (gate)** are our own design. Justifications follow.

## Scoring rules and the reason for every cutoff

### Tolerance (GL1–GL13)
Sum of points, range 13–47. The bands are the ones published on the Rutgers scoring grid:
13–18 → 1 (low), 19–22 → 2 (below average), 23–28 → 3 (average), 29–32 → 4 (above average),
33–47 → 5 (high). Mapping band n to tolerance level n is our choice.

### Capacity (B2–B6)
The capacity answers are **averaged**, the way the risk-attitude items are summed, so that one weaker answer (an
irregular income, a short horizon) is outweighed by strong ones (savings, a small share of wealth, a long horizon).

* B2 horizon, B4 savings, B5 share of wealth and B6 income each give **points** (1–5). The **average** of the four
  decides a level (average below 1.5 → 1, below 2.5 → 2, below 3.5 → 3, below 4.5 → 4, else 5: the nearest level).
* **B3 (money needed back) is a hard cap and is not averaged.** It states in money how much of the amount the plan can
  lose, so a plan that must get its money back cannot be offered a risky portfolio because the other answers are good.
* **A short horizon is the one averaged answer that also caps on its own** (`capacity.answer_caps`, added after the twelve-user check of 20 Oct): a horizon under a year (B2 = `lt1`) limits the capacity to level 2 and 1–3 years to level 3, however good the other answers are; it still counts as points in the average, and longer horizons only count as points. Without it, big savings, a small share of wealth and a stable income averaged a one-year horizon up to capacity 4 (C4, 70% stocks). Empty `answer_caps` = pure averaging; the values are the old per-answer caps for the two shortest answers.
* `capacity_level = min(B3 cap, answer caps, level of the average)`. `capacity_binding_question` is B3 when it is at least as low as
  the average's level, otherwise the averaged answer with the fewest points (first in `B2, B4, B5, B6` order on a tie).
  `capacity_average` keeps the average. B1 (goal) is stored, not scored.
* Both lists (`hard_caps`, `aggregate.questions`) are in `config.yaml`: move a question between them to make it a hard
  cap (it then needs `option_caps`) or to average it (B3 then uses its loss-capacity band as points).

| Item | Answer → points | Reasoning |
|---|---|---|
| B2 horizon | <1y → 1, 1–3y → 3, 3–5y → 4, >5y → 5 | Equity drawdowns can take several years to recover. Short horizons cannot wait for a recovery. |
| B4 emergency fund | <3 months → 2, 3–6 → 4, >6 → 5 | 3–6 months is the usual emergency-fund guideline. Without it, a shock forces selling at a loss. |
| B5 share of wealth | >75% → 1, 50–75% → 3, 25–50% → 4, <25% → 5 | The more concentrated the wealth, the less a loss can be absorbed elsewhere. |
| B6 income | none → 3, irregular or unstable → 4, stable → 5 | An irregular income is a weaker cushion, not a reason to stop investing: it costs one point of five, which a long horizon, savings and a small share of wealth outweigh. |
| B3 loss capacity (hard cap) | <5% → 1, 5–10% → 2, 10–20% → 3, 20–35% → 4, ≥35% → 5 | Bands roughly match the severe-crash loss at each level's equity cap: C1 ~5%, C2 ~10%, C3 ~20%, C4 ~35%, C5 ~45%+. **Estimate; Agent A's stress tests should confirm it.** Lower bounds are inclusive. |

Examples (average → level): a long horizon, savings over 6 months, under 25% of wealth invested and an irregular income =
(5 + 5 + 5 + 4) / 4 = 4.75 → capacity 5; a horizon of 3–5 years, 3–6 months of savings, 25–50% of wealth and a stable
income = (4 + 4 + 4 + 5) / 4 = 4.25 → 4; under a year, under 3 months of savings, over 75% of wealth and an irregular
income = (1 + 2 + 1 + 4) / 4 = 2.0 → 2. Before 20 Oct the rule was "the lowest single cap wins" (an irregular income capped everyone at 3).

`loss_capacity = max(0, invested − needed) / invested`, clipped to [0, 1], with `invested > 0` enforced.

### Knowledge and experience (K1–K5)
Knowledge **never** changes the C-level. It only restricts complex products (crypto, leveraged/inverse):

- 3 correct **and** some experience → `full`
- 3 correct without experience, or 2 correct → `warning` (the UI must collect an acknowledgement; flag `complex_products_ack_required`)
- 0–1 correct → `blocked_complex` (flag `complex_products_blocked`; Agent C gets `blocked_product_types = [crypto_etp, crypto_spot]`)

"Some experience" means K4 includes bonds, stocks, derivatives or crypto, **or** K5 is anything other
than "I have not invested before".

### Gate (D1) and combining
- D1 = "No" (no loss acceptable) → `C1`, `lowest_category = True`, `binding_dimension = "gate"`, R1
  only, and the suitability stress-loss limit is set to 0.
- Otherwise `c_level = min(tolerance_level, capacity_level)`, and `binding_dimension` is whichever is
  lower (`"tolerance"` on a tie). `lowest_category` stays False for a C1 that comes from scoring.

| C | max R-level | equity cap | gold & commodities cap | crypto cap | illustrative crash loss |
|---|---|---|---|---|---|
| C1 | R1 | 0% | 0% | 0% | 5% |
| C2 | R2 | 0% | 0% | 0% | 10% |
| C3 | R3 | 40% | 10% | 0% | 20% |
| C4 | R4 | 70% | 15% | 0% | 35% |
| C5 | R5 | 90% | 20% | 5% | 45% |

C_n ↔ R_n is the standard suitability-matching convention. The crash losses are **illustrative**: they
are used only for explanation text and to design the B3 bands.

What each class can trade follows from two rules applied together. The **R-level** decides which
products are allowed (gold R3 from C3, silver and broad commodities R4 from C4, crypto R5 in C5 only,
and crypto also needs full product knowledge). The **class caps** decide how much of the portfolio
those products may take. Agent A and the Risk Engine both enforce the caps; Agent A copies them into
`DEFAULT_RISK_CLASS_POLICIES`, and `tests/risk_profile/test_risk_profile.py` keeps the two tables in sync.
The commodity and crypto caps are PROVISIONAL. They are sized as a diversifier sleeve (commodities) and
a small satellite (crypto, which falls about 60% in the capacity scenario).


### The Risk Game inside the category (docs/RISK_GAME_BLEND.md)
Optional: `build_risk_profile(..., game=GameInput(tolerance, loss_acceptance))`. Without a game everything above is unchanged
(the profile object, its JSON and its answers hash are identical). With one:

* `game_score` = mean of the game's `tolerance` and `loss_acceptance` (0-100 each; the tolerance alone when there is no loss
  acceptance); `game_level` = `1 + floor(score / 20)`, at most 5.
* `blended_level` = `round((1 - w) * t + w * game_level)` to the nearest class (a half rounds up), kept between 1 and 5, with
  `t` the questionnaire's tolerance level and `w` = `config.game.weight` (1/3: two thirds questionnaire, one third game).
* `c_level` = `C1` for the gate, else `C{min(capacity_level, blended_level)}`. With `may_raise` off the game can only lower the
  tolerance part: `min(capacity_level, t, blended_level)`.
* `questionnaire_class` is the category WITHOUT the game (`min(capacity_level, t)`, C1 for the gate). `binding_dimension` is
  `"game"` when the game moved the category compared with that; otherwise `gate`, `capacity` or `tolerance` as before.
* Switches: `game_weight` / `may_raise` arguments (None = `config.game`: `weight` 0.3333333333, must be between 0 and 0.5, and
  `may_raise` true). Weight 0 ignores the game everywhere: the profile is the one without a game. The game fields
  (`game_played`, `game_score`, `game_level`, `blended_level`, `questionnaire_class`) are in the JSON only when a game was applied
  (one `@model_serializer` on `RiskProfile`).
* The game is hypothetical and client-reported, like the questionnaire answers; it moves the tolerance part by at most one third
  of the distance to its level and never past the capacity level. It does not change knowledge or product access.
* Tests: `tests/risk_profile/test_risk_game.py` (the contract's table, properties over random investors and games).

### Consistency check (flag only)
GL1 (how a best friend would describe you) is the self-description item. The scenario items are GL2,
GL3, GL4, GL7, GL8, GL11, GL12 and GL13. Each item is rescaled to 0–1 because items have 2–4 options.
If |GL1 − mean(scenarios)| ≥ 0.5, the flag `review_suggested` is raised. Scores are never changed.

Excluded: **GL9/GL10**, the gain-framed / loss-framed pair (verified in the Rutgers PDF: "given $1,000
… sure gain of $500" vs "given $2,000 … sure loss of $500"). A different choice across the two is the
classic framing effect, i.e. normal loss aversion. GL5 (comfort with stocks) and GL6 (word
association) are excluded because they are not scenarios.

### Audit log
`audit_records(answers, bank, timestamp)` returns one `AnswerAuditRecord` per answer
(`question_id`, `questionnaire_version`, `option_id`, and `value` for the numeric B3 amounts, plus
`timestamp`). A multi-select K4 answer gives one record per selected option.

### Compliance record
Every `RiskProfile` carries three fields:
- `assessed_at`: supplied by the caller (timezone-aware, stored in UTC; there is no clock inside scoring);
- `valid_until`: `assessed_at` plus `compliance.validity_months` (12, PROVISIONAL), counted in calendar
  months, with 29 Feb rolling back to 28 Feb;
- `answers_hash`: `sha256:` of the normalized answers and questionnaire version. Choices are sorted,
  multi-select order does not matter, and amounts are written with two decimals, so the same answers always
  give the same hash.

`run_pipeline(..., assessed_at=...)` defaults to midnight UTC on `as_of`. If `valid_until` is before
`as_of`, the result has `profile_expired=True`, and the client report shows a banner offering
re-assessment. The portfolio is still produced and the Risk Engine verdict is unchanged: an expired
profile is **flagged, not silently used or blocked**. The report always shows "Risk profile assessed on
<date>, valid until <date>".

## Open decisions (all in `config.yaml`)
1. Every cutoff above is PROVISIONAL and not bank-approved.
2. C2 equity cap is 0%; an R2 bond-heavy mixed fund may be allowed later.
3. One equity-cap table: Agent A's `DEFAULT_RISK_CLASS_POLICIES` was aligned to this config on `main`
   (commit 6017058) and a test keeps them in sync. Needs team sign-off.
4. Under the gate, `max_stress_loss = 0.0`. In Agent A's scenario, bonds fall 5%, so only
   cash-like assets are feasible.
5. Consistency threshold 0.5 and the choice of scenario items.
6. The "some experience" definition, and 3 correct without experience → `warning`.
7. Complex product list for Agent C (`crypto_etp`, `crypto_spot`). Leveraged/inverse products are not
   in Agent C's catalog yet.
8. Illustrative crash losses per C-level should be replaced with Agent A's stress results.
9. Stress scenario name (`equity_down_30_bond_down_5`) must stay in sync with `AgentConfig.capacity_scenario`.
   A test checks this.
10. `adapter.r1_max_asset_weight: 1.0`. Agent C's catalog has only two R1 funds (BIL, SHY), so
    Agent A's default 40% per-asset cap made every C1 portfolio infeasible. The cap is lifted for
    R1-only investors.
11. Product R-levels are Agent C's catalog values; the rule is written down in
    `egoquant/agents/market_intel/catalog.py` and checked by a test. SHY stays R1 (C1 would otherwise
    have one fund) and TLT R3 (long duration). Gold (IAU) is R3: its volatility is similar to a broad
    equity fund's.
13. Commodity and crypto caps per class (table above), plus Agent A's own cash cap (C3 30%, C4 20%,
    C5 15%, never below the investor's own cash need) so growth portfolios do not sit in T-bills.
12. Gate investors get a 0% loss limit in Agent A too (`InvestorContext.max_acceptable_loss` accepts 0),
    so their portfolio is held in cash-like assets that do not fall in the stress scenario.
