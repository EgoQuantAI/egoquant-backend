# Sandbox operation log: one simulated investor, start to finish

*Simulated account, invented data. Run on 20 Oct 2026 on the submitted code (tag `submission-20oct`, `381f52b`) in a safe test setup: a test account store, saved real prices (to 16 Oct 2026), offline sample news, a test blockchain inside the sandbox, no AI calls, no money.*

| # | Time (UTC) | Step | What happened | Result |
|---|---|---|---|---|
| 1 | 14:05:26 | Sign in | The simulated investor signs in (test account store, nobody real). | signed in (HTTP 200) |
| 2 | 14:05:26 | Consent | Agrees to the 'ibkr_data' terms (version 2026-10-19). | recorded |
| 3 | 14:05:26 | Consent | Agrees to the 'trade_check' terms (version 2026-10-19). | recorded |
| 4 | 14:05:26 | Risk questionnaire + Risk Game | Answers the 26 questions (invented 'balanced saver') and plays the Risk Game (tolerance 45, loss acceptance 40). | category C3 |
| 5 | 14:05:27 | Build and save the portfolio | The engine builds the portfolio on saved real prices (to 16 Oct 2026); the independent risk check re-checks it; it is saved to the account. | category C3, risk check approved; saved as report dac493cb |
| 6 | 14:05:27 | News rating to completion | Every headline about the holdings is rated (offline sample news; no AI call in the sandbox). | news job done: 8 headlines, 12 fund ratings, none left unrated (ratings from the saved sample set); final news saved into the report |
| 7 | 14:05:27 | Report viewed | The investor opens the saved report. | view recorded |
| 8 | 14:05:27 | Import past trades (IBKR) | Uploads the demo pack's INVENTED Interactive Brokers file (no real account). | 7 trades and 1 cash flow imported (2025-01-02 to 2026-05-04) |
| 9 | 14:05:28 | Trade check | Asks: 'buy 1,500 USD of VTI (US stocks)?' The rules answer, not the AI. | verdict: outside your limits (Your stock share is already above the limit of 40.0% (60.5%); this trade makes it 63.2%.) |
| 10 | 14:05:28 | Rebuild with the trading history | The portfolio is built again; the investor's own trading history can only make limits stricter. | behaviour limits before: 0, after: 1: 'Loss in a severe fall, behavioural limit', at most 8.8% (learned from past trading choices, confidence 80%) |
| 11 | 14:05:28 | Confirm and seal | The investor confirms the approved plan; only a 32-byte fingerprint is sent, here to a TEST chain inside the sandbox. | confirmed; seal confirmed (sandbox chain, block 1000001) |
| 12 | 14:05:28 | Verify the sealed report | The downloaded sealed report is checked against the chain; then a copy with one number changed (2.4% -> 99.9%). | original: Unchanged; changed copy: Does not match (report changed) |
| 13 | 14:05:28 | Delete my data | The investor deletes their data. | rows before: 29, after: 0 (sign-in and username stay, as designed) |

## What the system itself wrote in the account's activity log

| Time (UTC) | Action | Details |
|---|---|---|
| 2026-10-20T14:05:26 | consent_given | {"scope": "ibkr_data", "version": "2026-10-19"} |
| 2026-10-20T14:05:26 | consent_given | {"scope": "trade_check", "version": "2026-10-19"} |
| 2026-10-20T14:05:27 | questionnaire_submitted | {"report_id": "dac493cb-ec71-4db1-9ff5-f81e19d27e2f", "questionnaire_version": "eq-rq-0.1.0"} |
| 2026-10-20T14:05:27 | report_generated | {"report_id": "dac493cb-ec71-4db1-9ff5-f81e19d27e2f", "approved": true} |
| 2026-10-20T14:05:27 | report_viewed | {"report_id": "dac493cb-ec71-4db1-9ff5-f81e19d27e2f"} |
| 2026-10-20T14:05:27 | data_connected | {"source": "ibkr_report", "n_trades": 7, "n_cash_flows": 1, "start": "2025-01-02", "end": "2026-05-04", "currency": "USD"} |
| 2026-10-20T14:05:28 | trade_checked | {"check_id": "41591cd7-aed6-429d-a12c-d6afbc60ee0f", "verdict": "not_advisable", "ticker": "VTI", "side": "buy"} |
| 2026-10-20T14:05:28 | warning_shown | {"check_id": "41591cd7-aed6-429d-a12c-d6afbc60ee0f", "kind": "not_advisable"} |
| 2026-10-20T14:05:28 | questionnaire_submitted | {"report_id": "24f2def5-a333-49c4-9654-54ef1c7b1346", "questionnaire_version": "eq-rq-0.1.0"} |
| 2026-10-20T14:05:28 | report_generated | {"report_id": "24f2def5-a333-49c4-9654-54ef1c7b1346", "approved": true} |
| 2026-10-20T14:05:28 | plan_confirmed | {"report_id": "24f2def5-a333-49c4-9654-54ef1c7b1346"} |
| 2026-10-20T14:05:28 | record_sealed | {"report_id": "24f2def5-a333-49c4-9654-54ef1c7b1346", "tx_hash": "0xbb7f349b19600fca14c2ca7254dd7df0877a0780ba2c41e1db5f80f5c6388132"} |
