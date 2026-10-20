# Stress and hostile-user tools (Damla, 20 October 2026)

Small scripts, not part of the product and not run by `pytest`. All except the two probe scripts (`probe.py`, `probe_after_deploy.py`) need no network and no key: the local server reads the committed
price snapshot (`EGOQUANT_PRICE_MODE=snapshot`), and a throwaway signing secret is made for each run and written nowhere. The two probe scripts send read-only requests to a deployed URL that you give them.
Run them from the repository root with the project's Python. Evidence and numbers: `docs/judges/04_TESTING_AND_EVIDENCE.md` sections 5 to 9.

| Script | What it does |
|---|---|
| `serve.py <repo> <port>` | The app on a local port, snapshot prices, no keys. `HARNESS_PORTFOLIO_LIMIT` lifts the 20-a-minute limit for a test; `HARNESS_STACKS=<file>` writes every thread's stack every 45 s (for a hang). |
| `conc.py <repo> <port> alone rounds mixed leak flood` | Starts `serve.py`, then: **alone** builds 8 different investors twice (determinism baseline); **rounds** builds the 8 at once, 3 rounds, and compares every answer with the one built alone (sealed contexts opened and compared by their facts); **mixed** adds trade checks, profiles and forms beside the builds; **leak** builds 30 times in one process and prints the memory after each; **flood** starts 20 heavy goal builds at once (`FLOOD_N` changes the number) while polling `/health` and `/profile`. Memory is read from the server process (Windows). |
| `edge.py <repo>` | About 130 extreme but valid inputs through `/portfolio`: status, finite numbers, weights, verdict consistency. |
| `whatif_par.py <repo>` | Six what-if runs alone, then in parallel, compared. |
| `snaprace.py <repo> <rounds> [lock\|warm]` | 12 threads read one shared snapshot price table (fetch, hash, copy). Used to show that a native crash does not need our code (see section 8). Set `PYTHONPATH` to a folder with another pandas to compare versions. |
| `threadstart.py` | Starts thousands of threads beside busy numerical threads (section 9). |
| `probe.py <base_url> <out.json>` | The live probes: read-only, at most 115 requests counted inside the script, validation paths only, no portfolio build, no account, no AI call. Use only against a system you are allowed to test. |
| `probe_after_deploy.py <base_url> <out.json>` | The re-check after a deployment (18 checks A01 to A18, at most 27 requests, same rules as `probe.py`). The answers it expects are in its EXPECT table; 04 section 5 has the result of 20 Oct. |
