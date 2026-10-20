# Risk Game in the risk category (contract for four people; Savas, 20 Oct ~03:00 Dubai)

## 1. The rule (Savas)
* **Everybody is offered the Risk Game and is encouraged to play it**, whatever their risk cap and whether or not they have a trade log.
  A visible, confirmed "skip" exists; a skipped game means the questionnaire alone (the game then counts 0, the questionnaire 100) and the report says "Risk Game: not played".
* **In plain words.** Your *attitude to risk* is a number from 1 to 5: two thirds your questionnaire result plus one third your Risk
  Game result (both on the same 1 to 5 scale), rounded to a whole number. Your *capacity* is a second number from 1 to 5 that comes from
  your finances (how much you could lose, your horizon, your savings, your income). **Your risk category C1 to C5 is the LOWER of the two.**
  "I cannot accept any loss" still means C1. Nothing else about the questionnaire changes.
  (Technically: 66 and 33 are read as two thirds and one third, so equal levels give that level; with whole levels a rounding tie cannot happen.)
* **Behaviour beyond the game** (Agent B, trade log): unchanged and stronger. Real trading evidence still outranks the game; Agent B only
  ever tightens. Because the game now sits inside the category, Agent B stops using the game's two scores (`game`, `loss_acceptance`) as
  a SECOND tightening (that would count the game twice). The Crash Test stays what it is (soft, tighten-only evidence).
* Two settings, both changeable in the Railway variables without code. `EGOQUANT_RISK_GAME_WEIGHT` (default `0.3333`, one third) is the
  game's share; `0` switches the game off everywhere. `EGOQUANT_RISK_GAME_MAY_RAISE` (default on): set to `off` and the game can never make you
  bolder; then your category is the lowest of three numbers (your finances, your questionnaire result, and the blend of questionnaire and game).
* Words used in this document: the **category** (C1 most cautious to C5 most growth-oriented; the code calls it the class or `c_level`), the
  **capacity** (or cap: what your finances allow, 1 to 5), the **attitude** or tolerance (what you say and how you play, 1 to 5), the
  **blend** (two thirds questionnaire attitude plus one third game, rounded).

## 2. Definitions (fixed; do not re-derive)
*Section 2 is the exact formula, for engineers. The plain version is in section 1.*
* `game_score` = mean of the Risk Game's `tolerance` and `loss_acceptance` (0-100 each); `tolerance` alone when `loss_acceptance` is absent.
* `game_level` = 1 + floor(game_score / 20), at most 5 (equal bands: 0-19.99 -> 1, 20-39.99 -> 2, 40-59.99 -> 3, 60-79.99 -> 4, 80-100 -> 5).
* `t` = the questionnaire tolerance level (today's `tolerance_level`), `cap` = today's `capacity_level` (since 20 Oct: the average of the capacity answers B2/B4/B5/B6, with B3 "money needed back" as a hard cap; `fix/bartu-6`),
  `blend = (2*t + 1*game_level) / 3` (no game: `blend = t`), `blended_level = clamp(round(blend), 1, 5)`,
  `c_level = C1` if the gate, else `C{min(cap, blended_level)}` (with MAY_RAISE off: `min(cap, t, blended_level)`).
* `binding_dimension` gets a fourth value `"game"`: set whenever the final category differs from what the questionnaire alone would give
  (also when the cap equals the blend). Otherwise as today (`gate`, `capacity`, `tolerance`).

| t (questionnaire level 1-5) | game (game LEVEL 1-5, not the score) | cap (capacity level 1-5) | blend | class | note |
|---|---|---|---|---|---|
| 4 | 2 | 5 | 3.33 | C3 | cautious game lowers |
| 3 | 5 | 5 | 3.67 | C4 | bold game raises (MAY_RAISE off: C3) |
| 5 | 1 | 5 | 3.67 | C4 | |
| 5 | 5 | 3 | 5.00 | C3 | the cap wins |
| 2 | 5 | 5 | 3.00 | C3 | (MAY_RAISE off: C2) |
| 1 | 5 | 5 | 2.33 | C2 | |
| 3 | not played | 5 | 3.00 | C3 | unchanged from today |
| gate (the answer "I could not accept any loss") | any | any | - | C1 | |

## 3. API (backend; Onur)
* `AnswersIn` gets one optional field `risk_game: RiskGameIn | None` (same model as today: `tolerance`, `loss_acceptance`, `crash_test`,
  `completed_at`). Every endpoint that takes `answers` therefore sees the game and scores the SAME category: `/profile`, `/preferences/form`,
  `/preferences/areas`, `/portfolio`, `/account/portfolio`, `/trade-check`. The top-level `PortfolioIn.risk_game` stays for old clients
  and is used when `answers.risk_game` is absent. A result dated in the future (more than 10 minutes) or before 2024-01-01 is refused as an invalid request (HTTP 422); an old result is fine (the app keeps it in the browser, and sealed contexts are re-validated later), and sending it twice with different values (inside `answers` and at the top level) is a 422.
* `ProfileOut` gains (all optional, absent unless a game was sent): `game_played`, `game_score`, `game_level`, `blended_level`,
  `questionnaire_class` (the category WITHOUT the game). `c_level`, `max_r_level`, limits and eligibility are the blended ones.
* The report's class sentence and `Reason(kind="class")` say how the category was set ("questionnaire level 4, game level 2, combined 3,
  capacity cap 4: C3").

## 4. Lanes
| Who | Files | Task |
|---|---|---|
| **Bartu** | `egoquant/risk_profile/*`, `egoquant/agents/behavioral/*`, `egoquant/demo/personas.py`, their tests | `GameInput`, blend in `build_risk_profile(answers, bank, config, *, assessed_at, game=None, game_weight=None, may_raise=None)` (None = config defaults: weight 1/3, may_raise true), new profile fields (omitted from the JSON when no game: exactly ONE `@model_serializer` on `RiskProfile`), `binding_dimension` "game", `explain()` sentences, README; Agent B: with `BehaviorConfig.game_in_profile=True` ignore `game_result` and `loss_acceptance` evidence (keep crash test and trade log), facts say the game is part of the category; personas get optional game results; the example table above as tests, plus property tests (class <= cap, monotone in game level, no game = byte-identical profile, kill switch, gate) |
| **Onur** | `egoquant_routes.py`, `pipeline.py`, `report.py`, `settings.py`, `models.py`, docs | the API above, `Agent B` flag from the route, report sentences, settings functions, hostile-input tests, SECURITY.md / SUBMISSION_NOTES.md, golden stability |
| **Umut** | `fintechaton-frontend/**` | remove `gameRequired`; the game is a normal step for everyone, placed BEFORE the interests (the product list depends on the category), encouraged (plain English, what it does, 2 minutes), "skip" needs a confirmation that says the category will rest on the questionnaire alone; send `answers.risk_game` to `/profile`, `/preferences/form`, `/preferences/areas`, `/portfolio`, `/account/portfolio`, `/trade-check`; re-score the profile when the game ends or is replayed and re-check the chosen interests; review page and saved report show the breakdown; old saved sessions without a game show "Risk Game: not played" and the invitation |
| **Damla** | tests only (after her speed branches) | 300-investor property sweep over (answers x game x cap) through the real endpoints: class = min(cap, blend), never above the cap, deterministic; hostile inputs for `risk_game` (ranges, types, extra fields, future / stale dates, NaN); eligibility and limits follow the blended class on `/preferences/*`, `/portfolio`, `/trade-check`; sealed contexts still verify |

## 5. Order of work
1. Bartu pushes `feat/risk-game-profile` (the `build_risk_profile` signature above is fixed so Onur and Umut can start at once).
2. Onur pushes `feat/risk-game-routes` the moment the signature exists (stub allowed), merges both, full suite, then oct19.
3. Umut works against the contract on `fix/umut-7` and tests with the merged backend; Bartu then regenerates the demo pack with
   personas that play the game, Damla runs the sweep.
4. Nothing reaches `oct19` without the full suite and the golden tests green; with no game sent, every number stays identical.

## 6. Compliance notes (for SECURITY.md)
The Risk Game is hypothetical and client-reported, exactly like the questionnaire answers; it can move the tolerance part by at most one
third of the distance to its level and never beyond the capacity cap. A cautious game lowers the category; a bold one may raise it
(switch off with `EGOQUANT_RISK_GAME_MAY_RAISE=off`). The report always shows which parts set the category.
