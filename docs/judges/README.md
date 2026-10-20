# EgoQuant: documents for the judges

*2026 Shenzhen FinTechathon, WeBank International Track, Topic B: Wealth Advisory Agent. Status: 20 October 2026.*
Live system: https://fintechaton-frontend.vercel.app · API: https://web-production-ee5cc.up.railway.app (`/docs`) ·
Repositories: `fintechaton-backend` (Python, FastAPI) and `fintechaton-frontend` (Next.js), branch `oct19` (the frozen state is tagged).

## Five-minute path
1. Read the one-paragraph summary and the three promises in **[01 Project summary](01_PROJECT_SUMMARY.md)** (sections 1 to 3).
2. Try the product with **[05 User guide and demo](05_USER_GUIDE_AND_DEMO.md)**: the ten-minute path, a demo account, and the on-chain try-out.
3. Open **[02 Security and compliance report](02_SECURITY_COMPLIANCE_REPORT.md)**: the design in ten sentences (section 1) and the residual risks (section 11).
4. Skim **[07 Judges' questions](07_JUDGES_QA.md)** for the questions we expect, with evidence.

## All documents
| Document | What it answers | Read it if you are |
|---|---|---|
| [01 Project summary](01_PROJECT_SUMMARY.md) | What it is, the user's journey, architecture, how a portfolio is built, what is real and what is invented | everyone |
| [02 Security and compliance report](02_SECURITY_COMPLIANCE_REPORT.md) | Suitability, access control, privacy, AI governance, on-chain seal, resilience, evidence, residual risks | a security or compliance reviewer |
| [03 Methods and models, part A](03_METHODS_AND_MODELS.md) | Questionnaire and scoring, the Risk Game blend, behaviour (Agent B), preferences, market data (Agent C) | a technical reviewer |
| [03b Methods and models, part B](03b_METHODS_QUANT_RISK_ADVISOR.md) | The category limits table, portfolio construction (Agent A), the goal simulation and its calibration, the Risk Engine, the trade check, the report, the advisor | a technical reviewer |
| [04 Testing and evidence](04_TESTING_AND_EVIDENCE.md) | Test counts by area, red-teams, backtests, probes, QA findings and how to re-run everything | an auditor |
| [05 User guide and demo](05_USER_GUIDE_AND_DEMO.md) | The path through the product, with screenshots; demo accounts; the seal try-out | a judge using the system |
| [06 Limitations and roadmap](06_LIMITATIONS_AND_ROADMAP.md) | What it is not, where it is weak, what we measured, what we would do next | everyone |
| [07 Judges' questions](07_JUDGES_QA.md) | Short answers with evidence | everyone |
| [08 Frontend and API reference](08_FRONTEND_AND_API_REFERENCE.md) | Pages, the proxy, every endpoint, security headers | a technical reviewer |
| [09 The on-chain seal](09_ON_CHAIN_SEAL.md) | What is sealed and where it goes, what the seal proves and does not, how to check it in two minutes | a judge interested in the blockchain part |

## Existing deep-dive documents (in `docs/`)
`SECURITY.md` (threat table T1-T19 and 28 gaps) · `RISK_GAME_BLEND.md` · `V2_DECISION_SHEET.md` · `ANCHORING.md` · `ONCHAIN_DECISIONS.md` · `ON_CHAIN_EVIDENCE.md` · `JUDGE_GUIDE.md` · `DEPLOY_ENV.md` · `AGENT_D_LIMITATIONS.md` ·
`egoquant/risk_profile/README.md` · `egoquant/agents/behavioral/README.md` · `egoquant/briefing/README.md` · `eval/results/regime_v2_summary.md`.

## Three rules the whole system follows
1. **Numbers that matter come from deterministic, tested code; AI only explains.** (One labelled exception: Agent D's news-tone label, which never sets a number.)
2. **An independent check has the final word.** The Risk Engine re-computes every limit from the weights alone.
3. **We say what is not done.** Every claim here names a file, a test or a measured number; every weakness is listed.

*Everything is hypothetical and based on past data. Nothing here is a forecast or a recommendation to invest. EgoQuant places no orders and moves no money.*
