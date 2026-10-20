# Screens for the judges' report

Thirteen screens, each at 1440 x 900 (`-desktop.png`) and at phone size 390 x 844 (`-phone.png`). The phone shots
show the screen as it is first drawn; where the important part sits lower on a phone (game result, trade check, advisor card, verify)
the page is scrolled to it.

| File | Screen |
|---|---|
| `01-home` | A new person's welcome (agreement and "Start risk profile") |
| `01b-home-overview` | Home after the first portfolio: category, the category line, the portfolio check, goal chance |
| `02-questionnaire` | One question of the risk questionnaire (8 of 26) |
| `03-game-result` | The Risk Game result and what it did to the category (the category line shows the two thirds / one third blend) |
| `04-interests-counter` | Investments step with the "8 of 8 areas used for prices and news" counter and the numbered areas |
| `05-portfolio-summary` | Portfolio summary tab: verdict, category line, goal chance, stocks share, volatility, worst case |
| `06-goal-tab` | Goal tab: the chance as 100 boxes, where the portfolio could end up, how bad a miss could be |
| `07-trade-check` | Trade check verdict ("Outside your limits") with the invented example history |
| `08-saved-report` | A saved report opened from the account (the sidebar shows the "Verify a report" item in every screen) |
| `09-advisor-trade-card` | The Advisor answering a trade question, with the trade-check card under the reply |
| `10-verify-proof` | "Verify a report" in three steps, with the page's own demo report: a real report sealed on the Sepolia test network, every check passes, with who it was sealed for |
| `11-limits-tab` | Limits tab: each limit with where it comes from ("your category C4 and your answers", "the standard limit of 40% on any one holding") |
| `12-account` | The Account page: Saved reports (each with open / delete) and My seals, as a person with sealing switched on and nothing sealed yet sees them |

## What is real, and what is a stand-in

Everything on screen is the product itself: a real questionnaire, a real game played in the browser (a middle card each round, then the
crash test), a real portfolio build on the committed snapshot of real prices, the real engine's limits, verdicts and numbers. The parts
that are a stand-in for something a screenshot session cannot provide:

- **Signed-in person and database.** The signed-in person is a test account on a stand-in for the database, so the saved report's "Saved
  19 Oct 2026, 04:00" is a constant of that stand-in, not the time of the build.
- **Trade history (`07`, `09`).** The trade checks use the invented example history "Balanced investor", labelled "Invented example,
  not a real account" on the page.
- **Advisor reply (`09`).** A trade question can only be answered by the real language model, which these screenshots do not call. The
  text of the reply and the suggested follow-up are canned; the card under it is the real engine's check (the same numbers
  as the Trade check page, from the example history) shown by the real card.
- **Verify (`10`).** The report is the page's own demo report (an invented investor on real market data), sealed on the Sepolia test network
  (transaction `0x465e9db1...dbd78`); the page hashes it in the browser and the transaction was read from the public chain at the time of the
  screenshot. The sealing address on the page is the one in that report's seal.
- **Account (`12`).** The two saved reports are two real builds of the same portfolio (so they are identical), dated by the stand-in database. The test setup
  does not run sealing, so the page was shown the public sealing address to switch the seal section on; "My seals" is the page's own empty state.
- **No news (`NEWS` tab not shown).** The news briefing needs the live language model and is not part of these screens.
