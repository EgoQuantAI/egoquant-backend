# On-chain sealing: merge gate results (for Onur)

Prepared 19 Oct 2026, evening (Dubai), by the on-chain agent. Every result below was produced locally on Bartu's Mac,
offline (`pytest -m "not live"`, so no network and no real key).

## 1. Clean-copy merge test

| Run | Commit | Result |
|---|---|---|
| `onchain` with `origin/oct19` merged in (an ordinary merge, now pushed) | `7509737` (oct19 `60fc8e3` merged) | **2538 passed, 8 failed**, 21 deselected |
| plain `origin/oct19`, no on-chain code | `60fc8e3` | **2143 passed, 8 failed**, 20 deselected |

- The failing tests are **identical (byte-for-byte the same list)** in both runs:
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_analyze_goal_is_byte_identical[0]`
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_analyze_goal_is_byte_identical[off]`
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_analyze_goal_is_byte_identical[false]`
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_analyze_goal_is_byte_identical[no]`
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_analyze_goal_is_byte_identical[]`
  - `tests/regime/test_regime_goal.py::test_flag_off_golden_pipeline_probabilities_input_hash_report_and_html`
  - `tests/regime/test_regime_goal.py::test_pipeline_request_succeeds_with_the_flag_on_and_records_the_fallback`
  - `tests/system/test_what_if.py::test_a_scenario_that_breaks_a_cap_is_blocked_without_numbers`
- So no failure is caused by the on-chain work. The 7 in `tests/regime/test_regime_goal.py` are the known
  regime ones (Zerya/Damla). `tests/system/test_what_if.py::test_a_scenario_that_breaks_a_cap_is_blocked_without_numbers`
  also fails on plain `oct19`, so it isn't ours either (what-if owner).
- 2538 − 2143 = **395**, exactly the on-chain tests (`tests/anchoring`). The 1 extra deselected test is our live
  test (`tests/anchoring/test_live_seal.py`, `-m live`).
- An earlier run against `oct19` `1604085` gave the same picture: 2182 vs 1790 passed, and the same 8 failures.

## 2. Dependency audit (`eth-account==0.14.0`)

- **Setup:** a new, empty venv (`python3.14 -m venv`), then `pip install -r requirements.txt`. That installs the
  full deploy set, eth-account included (78 packages). The audit was `pip-audit 2.10.1 --path <that venv's
  site-packages>`.
- **Result: "No known vulnerabilities found"**, for all 78 packages, eth-account's tree included.
- **What eth-account adds** (no existing pin changes; `requirements.txt` differs from `oct19` by this one line):
  eth-account 0.14.0, eth-abi 6.0.0, eth-hash 0.8.0, eth-keyfile 0.10.0, eth-keys 0.8.0, eth-rlp 3.0.0,
  eth-typing 6.0.0, eth-utils 6.0.0, hexbytes 2.0.0, rlp 5.0.0, ckzg 2.1.8, pycryptodome 3.24.1, bitarray 3.12.3,
  cytoolz 1.2.0, toolz 1.2.0, parsimonious 0.10.0, py-ecc 8.0.0, regex 2026.9.29.
- **Python version, honestly:** `.python-version` says **3.14.4**, while this Mac only has **3.14.0**. Both use the
  same cp314 wheels for these packages, so the installed set and the audit are the same. Installing 3.14.4 just
  for this would have meant a system installer.

## 3. Your conditions, and the tests that prove them

| Condition | Where | Proved by |
|---|---|---|
| **Off** unless `EGOQ_SEAL_ENABLED=1` **and** a valid key (and `EGOQ_SEAL_RPC_URLS`) are set. When off, `/anchoring/status` says `{"enabled": false}` and every other anchoring route answers 503 (the frontend then hides the Seal section). | `client.anchor_client_from_env`, `routes._require_client` | `tests/anchoring/test_client.py::test_from_env_is_none_unless_enabled_and_keyed`, `tests/anchoring/test_routes.py::test_not_configured_hides_everything` |
| **No key** in any log, response, repr, result or stored row | key only inside the `eth_account` object; errors become labels | `tests/anchoring/test_routes.py::test_the_key_never_appears_in_any_log_or_response` (real signing, RPC down then retry, receipt, proof, verify), `tests/anchoring/test_client.py::test_the_key_never_appears_in_repr_logs_or_results`, `test_mainnet_and_bad_keys_are_refused_without_echo`, `tests/anchoring/test_wallet_script.py::test_creates_key_prints_only_address` |
| **No RPC URL or RPC error text** in logs or responses | `Rpc.call` turns node errors into labels (`rpc_down`, `rejected`, `nonce`); URLs come only from env on the server | `test_routes.py::test_the_key_never_appears_in_any_log_or_response` (asserts that neither RPC host appears in logs or bodies), `test_routes.py::test_verify_rpc_down_is_503_and_bad_proofs_422_without_echo`, `test_client.py::test_from_env_is_none_unless_enabled_and_keyed` (no `EGOQ_SEAL_RPC_URLS` means off) |
| **The signer refuses every chain except 11155111** | `SepoliaAnchorClient.__init__` (`config.ALLOWED_CHAIN_IDS`) | `test_client.py::test_mainnet_and_bad_keys_are_refused_without_echo`, `test_client.py::test_from_env_is_none_unless_enabled_and_keyed` (`EGOQ_SEAL_CHAIN_ID=1` means off), `test_client.py::test_tx_fields_self_send_zero_value_sepolia_and_exact_data` |
| **`/verify` stores nothing and logs nothing** | `routes.verify`, `routes.verify_tx` | `test_routes.py::test_verify_stores_and_logs_nothing` |
| **`/verify`: strict hash, 5 s chain budget** | `client.TX_HASH` (`^0x[0-9a-f]{64}$`), `client.budget(5)` | `test_routes.py::test_verify_tx` (bad hashes give 422), `test_client.py::test_read_tx_refuses_malformed_hashes_without_calling`, `test_client.py::test_budget_caps_total_rpc_time_across_urls` |
| **`/verify`: per-IP and global rate caps** (20/min per IP, 120/min in total, PROVISIONAL) | `routes._verify_limit` | `test_routes.py::test_verify_rate_limits_per_ip_and_global` |
| **`/verify`: size-limited** | `main.py` `BodySizeLimit` (yours). Today `/verify` gets the 1 MB default; with W2 it joins the 4.5 MB group, because a proof file carries the saved report. The `/verify` *page* never posts a proof: it hashes in the browser and sends only the tx hash. | `tests/test_body_limit.py` (the middleware). We have no `/verify`-specific size test, because `main.py` doesn't include the router yet. |
| Rejected reports are never confirmed or sealed; a second confirm returns the same record; another user's report gives 404; RPC down still confirms | `routes.confirm_plan` | `test_routes.py::test_rejected_report_409_nothing_stored_or_sent`, `::test_second_confirm_returns_the_same_record`, `::test_other_users_report_is_404_everywhere`, `::test_rpc_down_still_confirms_then_recovers` |
| Only `EGOQ1` + 32 bytes go on-chain | `commit.tx_data` | `test_proof_cli.py::test_only_prefix_and_commitment_go_on_chain`, `test_routes.py::test_full_flow_confirm_submitted_confirmed_proof_verify` |

## 4. Frontend (branch `onchain`, merged with `oct19`, pushed)

- **Wired** (Umut's lane, done with his authority through Bartu): `<SealSection>` on the portfolio Summary for saved
  reports only. It's hidden when sealing is off, the plan was rejected, or there is no saved report (a visitor).
  `POST /api/account/reports/{id}/viewed` is sent once when a saved report is shown. "Verify a proof file" is in the
  sidebar footer.
- `npx tsc --noEmit`, `npx eslint src` and `npx next build` are clean. `node scripts/check-vectors.mjs`: 310 checks
  pass (the browser serializer and hashes match the backend, including a real report's fingerprint).
- Screenshot `~/Documents/GitHub/onchain-evidence/08-portfolio-page-seal-section.jpg` shows the page with real market
  data. 05–07 show the confirm modal, "Sealing…" and "Sealed on Sepolia ✓".

## 5. Still Onur's (as in the step-4 entry)

- `main.py`: include `egoquant.anchoring.routes.router` and add `"/verify"` to the 4.5 MB body-limit group.
- The sealed-report 409 for a briefing save is **already done** on `oct19` (`60fc8e3`), and our merged suite passes
  with it.
- `supabase/migrations/006_onchain.sql`: copy from `docs/anchoring/006_onchain.PROPOSED.sql` after review. Bideci runs
  it after 005.
