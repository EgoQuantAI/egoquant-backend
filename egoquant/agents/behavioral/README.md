# Agent B: Behavioral Twin (v0.2)

Agent B looks at what an investor actually DID (trading logs) and how they played the
Eckel-Grossman risk game, and turns that into numeric limits Agent A can enforce, plus facts and
flags the Advisor can explain. It is deterministic and rule-based: same inputs, same output, no LLM.

## Files

| File | What it is |
|---|---|
| `egoquant/agents/behavioral/core.py` | Pure math: returns, drawdowns, panic episodes, cost of panic, disposition effect, chasing, holding period. |
| `egoquant/agents/behavioral/models.py` | B's own models: `TradingHistory`, `GameResult`, `BehavioralData`, `BehaviorConfig`, `BehavioralReport`. |
| `egoquant/agents/behavioral/agent.py` | `BehavioralTwinAgent`, which implements Agent A's `BehavioralAdvisor` protocol. |
| `egoquant/demo/synthetic_investors.py` | SYNTHETIC market path, 8 synthetic investors, in-memory data provider. |
| `tests/agent_b/test_behavioral.py` | Tests, most of them end to end through the real Agent A. |
| `python -m egoquant behavior` | Demo: Agent A + Agent B on the 8 synthetic investors. |

## Plug it in

```python
from egoquant.agents.behavioral import BehavioralTwinAgent
from egoquant.agents.quant import QuantAgent
from egoquant.demo.synthetic_investors import synthetic_behavioral_provider

agent_b = BehavioralTwinAgent(synthetic_behavioral_provider())   # replaces StubBehavioralAgent
agent_a = QuantAgent(market_provider, agent_b, config)
result = agent_a.run(context, as_of)                              # A calls B.evaluate_menu itself
report = agent_b.analyze(context.investor_id, result.menu)        # full report for the Advisor
```

For real data, write one method `get_behavioral_data(investor_id) -> BehavioralData | None`.
If the orchestrator gives Agent A custom C1-C5 policies, pass the same table to
`BehaviorConfig(risk_class_policies=...)` so A and B agree.

## Inputs

- **Trading logs** (`TradingHistory`): daily position values (cash must be a column), deposits and
  withdrawals, trades, the class of each asset. Optional: daily `asset_prices` (needed for cost of
  panic, disposition, return chasing, hype chasing) and `fundamentals` tags (needed for hype chasing).
- **Game** (`GameResult`): exactly the object in the team document (choice 1-6, payoffs, timestamp).
- **Stated horizon** (`stated_horizon_years`): only for the horizon-mismatch flag.
- **From Agent A's menu**: the questionnaire class (C1-C5), the class target equity and each
  portfolio's stress losses and (when holdings are known) turnover.

Anything dated after the menu's `as_of` is ignored. Bad data never crashes the pipeline: the report
says `invalid_data`, lists the reasons, and sends no limits.

## Limits B sends to Agent A

| Behavior | Rule | Limit | Strength |
|---|---|---|---|
| Panic selling | Net-sold >= 30% of equity within 10 trading days while already down >= 5% (selling to fund a withdrawal does not count; one episode per bad patch) | `max_stress_loss` in the crash scenario = median loss at which they sold | 2+ consistent episodes: **hard**, 0.8 (0.9 for 3+). 1 episode: soft 0.6. 1 episode + game also more cautious than the class: soft **0.7**. Held through a deeper drop another time: 0.45, so Agent A ignores it and says so |
| Eckel-Grossman game | Only when the logs show no panic and no meaningful drop (new or calm-market investors), and the game level is **2+ below** the questionnaire class | `max_stress_loss` = **Agent A's own limit for the game's level** (e.g. game level 1 -> C1's limit) | soft, 0.55 |
| Loss acceptance (app Risk Game) | Score 0-100: after losing rounds, did the player pick safer (0) or riskier (100) bets? Bands of 20 give level 1-5. Acts only below the neutral 50 (the app sends 50 when no round was lost), with no informative trading logs, and when the level is **2+ below** the class | `max_stress_loss` = Agent A's own limit for that level; with a cautious game too, the more cautious level wins | soft, 0.55 (0.60 with the game) |
| Overtrading | One-way turnover above 100% a year | `max_turnover` = 20% per rebalance (needs current holdings) | soft, 0.6 |

Input `BehavioralData.loss_acceptance: LossAcceptance(score, rounds_after_loss, timestamp)`; `rounds_after_loss=0` is ignored. All bands PROVISIONAL (`BehaviorConfig.loss_acceptance_*`).

Game level: choice 1-4 -> level 1-4, choices 5 and 6 -> level 5. A game that is only 1 level more
cautious, or more aggressive, is a flag only. B never loosens anything.
Evidence that is no stricter than the questionnaire class's own loss limit (for example sales at a 5.7%
fall for a C1 investor, whose class limit is 5%) sends no limit at all (flag `behavioral_limit_not_below_class`);
the measured threshold is still shown as a feature.

**Never unreachable.** If even the safest menu portfolio loses more than the evidence says, the
loss limit is clamped to that portfolio's loss (flag `behavioral_limit_clamped_to_menu`). If B
sends both a loss limit and a turnover limit, the turnover limit is widened just enough that the
loss limit can be met (flag `turnover_limit_widened_for_loss_limit`); otherwise Agent A would drop
all soft limits together.

## Advisor-only patterns (facts and flags, never limits)

| Pattern | How it is measured | Flag |
|---|---|---|
| Cost of panic | Value of simply holding the pre-sale positions for 1 year minus what actually happened | `panic_selling_was_costly` (> 2% of the account) |
| Disposition effect | Odean (1998): share of gains realized minus share of losses realized, counted on sell days | `disposition_effect` (> 0.10, 5+ sell days) |
| Return chasing | Share of discretionary equity buys made after a top-20% 20-day rally (about 20% by chance; deposit-day buys excluded) | `return_chasing` (>= 40%, 10+ buys) |
| Hype chasing | Buys of a stock that is unprofitable or has debt/equity > 2, right after a 25%+ 20-day spike | `hype_chasing` (2+ such buys) |
| Horizon mismatch | Holding period implied by selling speed vs the stated horizon | `horizon_mismatch` (< half the horizon) |
| Concentration | 90th percentile of the largest position's weight | `high_concentration` (> 50%) |

Every fact the Advisor can say is in `report.advisor_facts`, with the evidence in `report.evidence`.

## Synthetic investors (all labeled synthetic)

| Profile | Class / game | What B does | Final portfolio (Agent A) |
|---|---|---|---|
| steady | C4 / 4 | Held through -20% twice: no limit | 60% equity, -18% in the crash |
| panic_seller | C4 / 3 | Sold at -11.3% and -13.1%: hard 12.2% limit; each sale cost 5-6% | 36% equity, -12.0% |
| sara | C3 / 2 | One sale at -9.1% + cautious game: soft 0.7 | 24% equity, -9.0% |
| overtrader | C4 / 5 | 263% turnover a year: 20% turnover cap | 60% equity, 20% of the portfolio moved |
| disposition | C3 / 3 | Realizes 58% of gains vs 10% of losses: flag | unchanged |
| hype_chaser | C4 / 6 | 81% of buys after rallies, 5 hype buys: flags | unchanged |
| newcomer | C4 / 1 | No logs, game 3 levels below: soft C1 limit (5%) | 8% equity, -5.0% |
| thin_history | C3 / none | Too little data: no limit | unchanged |

## Known limitations (for the security self-assessment)

- Comparing an instantaneous stress-scenario loss with a realized drawdown is an approximation.
- The game is hypothetical and gains-only; it never makes a limit hard and real trades outrank it.
- Positions held before the logs start get their first visible price as cost basis (disposition).
- Rally percentiles use the asset's whole visible history (fine for describing past behavior).
- `implied_risk_aversion` rests on an assumed equity premium and volatility; diagnostic only.
- The synthetic market includes invented crashes, rebounds and a hyped stock; numbers say nothing
  about real markets.
