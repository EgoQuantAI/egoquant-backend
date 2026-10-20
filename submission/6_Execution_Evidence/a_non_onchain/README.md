# What we ran, and the proof (no real money, no real people)

A **simulated account** is an invented investor: the name, the answers and the trading history are made up, and nobody real is
behind them. **Sandbox** means a safe test setup: nothing is ever bought or sold, the data is test data, and the "blockchain" step
uses a test chain. We ran one simulated investor through EgoQuant from the first click to "delete my data", on the exact code we
submitted. It shows the advisor working end to end, step by step, and what it wrote down at each step.

## Follow one simulated investor

Every step below is one line of [sandbox_operation_log.md](sandbox_operation_log.md) (the # column).

1. Signed in to a test account (#1) and agreed to the data and trade-check terms (#2, #3).
2. Answered the risk questionnaire (25 questions, 26 answers) and played the Risk Game → **category C3 (balanced)** (#4).
3. EgoQuant built a portfolio on saved real market prices; the independent risk check **approved** it, and it was saved (#5).
4. The news about each fund was rated to the end: 8 headlines, 12 ratings, **none left unrated** (#6).
5. The investor opened the saved report (#7). The report itself: [sandbox_report.html](sandbox_report.html).
6. Uploaded an **invented** Interactive Brokers file: 7 trades and 1 cash payment imported (#8).
7. Asked "should I buy 1,500 USD of VTI (US stocks)?" → **outside your limits**: the investor's current holdings (from the imported file) are already 60.5% stocks, above the C3 limit of 40%, and this trade would make it 63.2%. The rules decided this, not the AI (#9).
8. Rebuilt the portfolio with the trading history → one **stricter** limit appeared: a loss in a severe fall of at most 8.8%,
   learned from past behaviour. History can only make limits stricter, never looser (#10).
9. Confirmed the approved plan → it was **sealed** (a 32-byte fingerprint on the sandbox test chain) (#11).
10. Checked the downloaded sealed report → **Unchanged**; changed one number in a copy → **Does not match** (#12).
11. Pressed "Delete my data" → **29 data rows before, 0 after**; only the sign-in and username stay, as designed (#13).

The same run, one line per step plus the activity rows the system wrote itself (questionnaire_submitted, report_generated,
report_viewed, data_connected, trade_checked, plan_confirmed, record_sealed, ...): [sandbox_operation_log.jsonl](sandbox_operation_log.jsonl).
The raw output: [sandbox_run_console.log](sandbox_run_console.log).

## Other evidence that the system ran

| File | In one sentence |
|---|---|
| [../deploy_check_live_new_service_59960_20oct_1744.txt](../deploy_check_live_new_service_59960_20oct_1744.txt), [../deploy_check_live_after_final_deploy.txt](../deploy_check_live_after_final_deploy.txt), [../deploy_check_live_20oct_1604.txt](../deploy_check_live_20oct_1604.txt) | Read-only checks of the live service on 20 Oct: **71 of 71 passed** each time, including real portfolio builds for test investors. |
| [../data_status_live_new_service_59960_20oct_1744.json](../data_status_live_new_service_59960_20oct_1744.json) (and the two earlier `data_status_live_*.json`) | What the live service reported about its data and settings at that moment (no secrets in it). |
| [../fourteen_investors_20oct_texts.md](../fourteen_investors_20oct_texts.md), [../fourteen_investors_20oct_data.json](../fourteen_investors_20oct_data.json), [../fourteen_investors_20oct_console.log](../fourteen_investors_20oct_console.log) | Fourteen invented investors run through the advisor, with every sentence they would read. |
| [../test_run_by_area_20oct.txt](../test_run_by_area_20oct.txt) | The full automated test run: **3,538 passed of 3,540** selected (1 known timing test failed, 1 skipped). |
| [../committed/chat_redteam_20261020_164237.jsonl](../committed/chat_redteam_20261020_164237.jsonl) | Hostile questions to the AI advisor (asking for buy/sell tips, personal data, made-up numbers): **41 of 42 passed**. |
| [../committed/trade_explain_redteam_20261020_162406.jsonl](../committed/trade_explain_redteam_20261020_162406.jsonl) | The same for the plain-English trade explanations: **51 of 51 passed**. |
| [../committed/SUPABASE_LOG.md](../committed/SUPABASE_LOG.md) | How the account database was set up and locked down, step by step. |
| Demo video | Recorded (about 5 minutes), submitted with this package; narration: [../../DEMO_VIDEO_NARRATION.md](../../DEMO_VIDEO_NARRATION.md). |

## Check it yourself

Open **https://www.egoquant.app**. Click **I. Profile** and answer the questions, then **IV. Portfolio** → **Build Portfolio**: you get your own
checked plan in a few seconds. Or click **Verify a report** → **Use the demo report** to check a real sealed report.

## What this does not show

No real broker account, no real money and no real person: the sandbox investor and its trades are invented, and its seal went to a
test chain inside the sandbox (real seals on the public test network are in [../b_onchain/](../b_onchain/README.md)).
