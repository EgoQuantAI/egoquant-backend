# EgoQuant: demo video script (about 5 minutes)

One continuous story with one invented investor (the balanced saver of `docs/DEMO_SCRIPT.md`, scenario 2), plus a 30-second cut-in
with the eager beginner. Every number below is read off the same run as the demo script (`docs/demo_data/demo_numbers.json`, built by
`python eval/demo_pack.py`; this file by `python eval/build_demo_doc.py --template eval/demo_video.template.md --out docs/DEMO_VIDEO_SCRIPT.md`),
so the narration can be read aloud as written. The wording around the numbers is the template's.
Portfolio method: the default one (v1). The run's decision date is pinned to 2026-10-16 (the last close in the saved prices) so that it can be repeated; the live app uses the last weekday before the day it is used, so its page may show a later date with the same prices.

## Shot list

| # | Time | Scene | Screen |
|---|---|---|---|
| 1 | 0:00-0:25 | The promise | Home page |
| 2 | 0:25-0:55 | 25 questions, no AI scoring | Risk Profile page, then its result |
| 3 | 0:55-1:35 | The Risk Game is part of the category | Risk Game page, then the result |
| 4 | 1:35-2:05 | Only what you may hold, and why | Investments page |
| 5 | 2:05-3:05 | The portfolio, and the independent check | Portfolio page, Summary tab |
| 6 | 3:05-3:50 | Behaviour can only tighten | Investments page (history upload), Portfolio page, Limits tab |
| 7 | 3:50-4:35 | Ask it, check a trade | Advisor panel, Trade check tab |
| 8 | 4:35-5:05 | Seal and verify | Seal button, Etherscan, Verify a report |
| 9 | 5:05-5:35 | Optional cut-in: the eager beginner | Result pages of scenario 3 |
| 10 | 5:35-5:55 | The honest close | Title card |

Target length 5:30 to 6:00 with the cut-in, 5:00 without it. Speak at a calm pace (about 140 words a minute): the narration below is about 750 words (700 without the optional cut-in of scene 9).

## Before you record

- A private window, an empty session, browser zoom 125%, screen 1920x1080, mouse moved slowly, no notifications, no key or token on screen.
- The three invented IBKR files from `docs/demo_data/` on the desktop; use `ibkr_balanced_investor.xml` in scene 6.
- Answer the questionnaire and play the Risk Game once beforehand to know the pages. In the recording, speed up the 25 clicks and the build
  wait (8x) and put a small "8x" label in the corner: the speed-up is shown, not hidden. A build takes about 5 to 7 seconds on the saved prices of the live site.
- Expected notes at the top of the portfolio page on the live site (price mode snapshot): *Prices: saved snapshot of real prices as of 16 Oct 2026 ...*
  and *DRAFT LIMITS ...*, both plain. If the prices ever come live, the weights may move by a few points; categories, limits
  and verdicts do not change.
- Subtitles: English burned in; Chinese subtitles if the judges read Chinese (Savas decides).
- Say "hypothetical" and "simulated" every time a chance or a loss is mentioned. Never say "we recommend", "you should" or "guaranteed".

---

## Scene 1 · 0:25 · The promise
**Screen:** home page, slowly scrolling the headline.
**Say:** "Most investment apps show you a portfolio and ask you to trust it. EgoQuant shows you a portfolio and shows you how to check it.
Every number comes from code you can test. The AI never chooses anything; it only explains. Let me show you with an invented investor."

## Scene 2 · 0:30 · 25 questions, no AI scoring
**Screen:** Risk Profile page: show question GL2 ("TV game show"), then speed through the rest at 8x, then the result page:
attitude level 3 of 5, financial situation allows level 5, category C3.
**Say:** "First, twenty-five questions. The thirteen about attitude to risk are a published scientific scale, and the three about
financial knowledge are a published literacy test, both quoted word for word. A fixed program scores them. No AI is involved. Our
investor scores attitude level 3 out of five, and their finances allow level 5. The category is the lower of the two."

## Scene 3 · 0:40 · The Risk Game is part of the category
**Screen:** Risk Game page, play two rounds, then the result: *it does not change the category: C3*. The result page says:
  > The Risk Game you played came to level 3 of 5 (score 50 out of 100). Together with your questionnaire (two thirds / one third) that is attitude level 3. Your financial situation sets a cap at level 5. Your category is the lower of the two: C3.
  > The game did not change your category: your questionnaire attitude level (3) sets it, C3.
**Say:** "Then a short game about choices under risk. It counts for one third of the attitude to risk, the questionnaire for two thirds,
and the category is the lower of that and what your finances allow. So a bold game can never lift you above what you can afford,
and a cautious game can lower you. For our investor, it does not change the category: C3. The report always says, in one sentence, how the
category was set."

## Scene 4 · 0:30 · Only what you may hold, and why
**Screen:** Investments page, platform **Interactive Brokers**, tick the choices, then point the cursor at the greyed-out options:
- P1: "Real estate (listed property companies, REITs)" is greyed out: riskier than your category C3 allows (these products are rated risk level R4 or higher; C3 allows up to R3).
- P1: "Silver, platinum and palladium" is greyed out: riskier than your category C3 allows (these products are rated risk level R4 or higher; C3 allows up to R3).
- P1: "Broad commodities (energy, industrial metals, agriculture)" is greyed out: riskier than your category C3 allows (these products are rated risk level R4 or higher; C3 allows up to R3).
- P2: "Technology" is greyed out: riskier than your category C3 allows (these products are rated risk level R4 or higher; C3 allows up to R3).
**Say:** "Now the investor says what they like. Look at what is greyed out. The page tells you why: this kind of product is riskier
than your category allows. Nobody can tick something they would then be refused. The choices decide what is looked at; they never
loosen a limit."

## Scene 5 · 1:00 · The portfolio, and the independent check
**Screen:** Portfolio page: horizon 10 years, goal 120,000, monthly 300, *Build Portfolio* (speed up the wait).
Summary tab: **Approved C3** · Goal Chance **87%** · In Stocks **23%** ·
Volatility **5.7%** · Worst Case **11%**. Mix: Stocks 23% · Bonds 50% · Cash 17% · Gold & commodities 10%.
**Say:** "The goal is 120,000 in 10 years, saving 300 a month. EgoQuant builds a hundred
candidate portfolios, keeps the ones that fit this person, and simulates ten thousand possible futures on past prices. The result:
87 of every 100 simulated futures reach the goal. That is a hypothetical simulation on past data, not a
forecast. Stocks are 23%, and in the harshest hypothetical scenario the loss is about 11%.
Then an independent Risk Engine, separate code with its own arithmetic, re-checked 19 of 19 limits. Only then does
the page say Approved. And read the two notes at the top: the first says these are saved real prices, the second says the limits are a
draft, not a bank's policy. We say it on the screen so you do not have to ask."

## Scene 6 · 0:45 · Behaviour can only tighten
**Screen:** Investments page, upload `ibkr_balanced_investor.xml` (read-only Flex report), build again. Summary: In Stocks **8%**,
Goal Chance **86%**. Limits tab, the new line: **Loss in a severe fall, behavioural limit: 6.6% ≤ 8.8%**.
**Say:** "Now the part we are proudest of. The investor connects a trading history, read-only: we never see a password and never
place an order. This invented account sold half its stocks in two market falls. A second agent, Agent B, reads that and can only make
the limits stricter, never looser. The loss limit moves from 20% for the category to 8.8% for this
person. Stocks go from 23% to 8%, and the goal chance fell from 87% to 86%. The Limits tab shows the new line, and the
report says why, in a sentence."

## Scene 7 · 0:45 · Ask it, check a trade
**Screen:** advisor panel. Ask *"What if I drop all my bond funds?"*: the answer is **blocked**. Ask *"What if I add $300 a month more?"*:
goal chance 86% → 100% (86 → 100 in 100 simulated futures). Then the Trade check tab: **Buy 5,000 of VT** → **outside your limits**:
  - *blocking*: Your stock share is already above the limit of 40.0% (60.5%); this trade makes it 69.6%.
  - *blocking*: The hypothetical loss in the scenario 'Severe crash (stocks -30%)' is already above the limit of 8.8% (19.2%); this trade makes it 21.9%.
  - *warning*: Your VT share is already above the limit of 40.0% (60.5%); this trade makes it 69.6%.
**Say:** "You can ask questions in plain words. The AI only translates your question into a scenario and words the answer; the
simulation is code. Ask for something that breaks a rule, like dropping every bond fund, and the Risk Engine says no, and no chance is
shown at all. Ask a fair one, like adding a monthly amount, and you get a before and after, side by side, in simulated futures. And
before you trade, you can check a trade against your own limits. Here is a purchase: Buy 5,000 of VT. The check says it is
outside your limits, and every reason and number comes from code. It is a check against your limits. It is not a recommendation,
and the page says so."

## Scene 8 · 0:30 · Seal and verify
**Screen:** sign in, *Confirm and seal*, then *Sealed on Sepolia*; open Etherscan; then *Verify a report*, *Use the demo report* and *Try a tampered copy*
(`docs/JUDGE_GUIDE.md`, five minutes).
**Say:** "Finally, trust. When you confirm a plan, EgoQuant publishes a fingerprint of the advice on a public test blockchain. Anyone can
check later that the report was not changed: change one number in the report and the check fails. The fingerprint holds
no personal data, and deleting your data removes the record and its salt."

## Scene 9 (optional) · 0:30 · The eager beginner
**Screen:** result pages of scenario 3 (`docs/DEMO_SCRIPT.md`): questionnaire alone **C5**, after the Risk Game **C4**;
the Investments page with crypto greyed out.
**Say:** "One more person. Bold answers, no experience. The questionnaire alone says C5. And then the game: it LOWERS the category from C5 to C4.
Crypto is not available to this person, because the knowledge questions were not answered correctly. Knowledge never changes the
category, but it does decide which complex products you are shown."

## Scene 10 · 0:20 · The honest close
**Screen:** title card with three lines: *Provisional limits. Hypothetical simulations. Not approved by a bank.*
**Say:** "Every limit in EgoQuant is a proposal, and every simulation is hypothetical and based on past data. We built it so that each
claim has a test behind it, and so that you can check us. Thank you."

---

## If something goes wrong while recording
- The build takes longer than 60 seconds: cut the wait (labelled 8x). If it fails with a message, read the message aloud: the messages are
  written for the investor.
- A number on screen differs by a few points from this script: say the number on the screen. Categories, verdicts and reasons do not change.
- The game scores differ slightly from the pack: the category shown is what counts; the script's category is for the pack's play.
