# EgoQuant source code

Two repositories, both at the version tagged **`submission-20oct`** (20 October 2026). The complete tagged trees are also in this folder as ZIP files, so the code can be read without GitHub access.

| Repository | What it holds | Link |
|---|---|---|
| `fintechaton-backend` | Python 3.14, FastAPI: the risk questionnaire and Risk Game scoring, Agents A (quant), B (behaviour), C (market data), D (news briefing), the Risk Engine, the trade check, the advisor, accounts (Supabase), the on-chain seal; `tests/` (3,540 offline tests in 141 files); `eval/` (deploy check, stress tools, red-teams, demo pack); `docs/` (every document, including `docs/judges/`) | https://github.com/EgoQuantAI/egoquant-backend (tag `submission-20oct`; a one-commit public copy of the tagged tree; the development history is private) |
| `fintechaton-frontend` | Next.js 16, React 19, Tailwind: the website, the server-side proxy that holds the session cookies, the Verify page that hashes a sealed report in the browser | https://github.com/EgoQuantAI/egoquant-frontend (tag `submission-20oct`; a one-commit public copy of the tagged tree; the development history is private) |

## How to run (short form; the READMEs have the long form)
- Backend: `pip install -e ".[dev]"`, then `uvicorn main:app`; every setting is an environment variable documented in `docs/DEPLOY_ENV.md` (with the kill switches and the post-deploy check `python eval/deploy_check.py <url>`). Offline tests: `pytest -q -m "not live"` (about 35 minutes; on Windows see `docs/judges/04_TESTING_AND_EVIDENCE.md` section 9).
- Frontend: Node.js 22, `npm install`, `npm run dev` with `BACKEND_URL` pointing at the backend; `npm run build` for production; `node scripts/check-vectors.mjs` checks the browser-side seal code against the backend (323 checks).
- Deployed: the frontend on Vercel, the backend on Railway, accounts on Supabase (migrations in `supabase/migrations/`, Row-Level Security proofs in `supabase/tests/`).

## Test cases
`tests/` by area: `agent_b` 680, `briefing` 414, `anchoring` 464, top level 407, `system` 360, `agent_a` 287, `accounts` 265, `agent_c` 274, `risk_profile` 134, `regime` 90, `advisor` 105, `preferences` 60 (counted by `pytest --collect-only -q -m "not live"` on the tagged version). The last complete run: 3,538 passed, 1 failed (a known timing test of the news page), 1 skipped (`6_Execution_Evidence/test_run_by_area_20oct.txt`).
