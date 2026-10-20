# On-chain sealing of confirmed advisory records: plan

Status: PLAN ONLY (19 Oct 2026). Nothing here is built yet. The build happens on branch `onchain`; the
step-by-step checklist lives outside the repo (`ON_CHAIN_RECIPE.md`).

## 1. What and why

When an investor confirms an approved EgoQuant plan, we write down exactly what we advised: their risk class
and answers, the allocation, the Risk Engine's checks, the model versions and the log of what they did. We
then publish a 32-byte fingerprint of that record on a public test blockchain (Ethereum Sepolia). The
fingerprint reveals nothing, and anyone can check it later. The investor keeps a "proof file". If anyone
(including us) later changes one weight, one answer or one log entry, the fingerprint no longer matches the
one fixed in a public block on a known date. This is tamper-evidence for advice, which regulators ask for. It
puts no personal data on-chain, never trades, and never delays the advice. In production the same fingerprint
format would go to FISCO BCOS, WeBank's consortium chain. For the competition we use Sepolia, whose
transaction hashes the organizers accept for the on-chain bonus.

## 2. The record (`egoq-record-1`)

One JSON object per confirmed report version. All values are fake in the examples below.

| Field | Type | Source in our code | Example |
|---|---|---|---|
| `schema` | string | constant | `"egoq-record-1"` |
| `report_id` | uuid string | `reports.id`, from `POST /account/portfolio` in `egoquant_routes.py` | `"00000000-0000-4000-8000-000000000001"` |
| `record_version` | int | 1 for the first confirmation of this report; +1 if the same report is confirmed again after a change (never today) | `1` |
| `confirmed_at` | UTC string `YYYY-MM-DDTHH:MM:SSZ` | server clock when "I confirm this plan" arrives | `"2026-10-20T08:15:30Z"` |
| `consent_version` | string | `egoquant.accounts.routes.CONSENT_VERSION` (the consent row written at confirm, scope `record_seal`) | `"2026-10-20"` |
| `investor.risk_class` | `C1`–`C5` | `report.risk_class` (`ClientReport`) | `"C3"` |
| `investor.questionnaire_version` | string | `BANK.questionnaire_version` | `"qv-test"` |
| `investor.answers` | object | the `AnswersIn` the report was built from. **Missing today:** `/account/portfolio` does not store the answers; it must save them with the report (see 4.4) | `{"choices": {"G1": "b"}, "multi_choices": {}, "amount_invested_cents": 1000000, "amount_needed_cents": 0}` |
| `allocation` | list sorted by ticker | `report.allocation[].asset_id/weight`, weight > 0 only | `[{"ticker": "BIL", "weight_ppm": 300000}, …]` (sums to 1 000 000) |
| `risk_engine.approved` | bool | `PortfolioOut.approved` (must be `true`; rejected reports are never sealed) | `true` |
| `risk_engine.violations` | list of strings | `PortfolioOut.violations` | `[]` |
| `risk_engine.checks` | list sorted by name | `PortfolioOut.checks` (`name, value, limit, direction, ok`) | `[{"name": "Share in stocks", "direction": "max", "ok": true, "value": "0.400000", "limit": "0.400000"}]` |
| `versions.engine` | string | `report.engine_version` | `"0.3.0"` |
| `versions.input_hash` | string | `report.input_hash` (Agent A's input fingerprint) | `"db076bb630a5b957"` |
| `versions.risk_profile_config` | string | risk_profile config version | `"test"` |
| `versions.agent_d_prompt_sha256` | hex | SHA-256 of `egoquant/briefing/prompts/fund_aware_tone.txt` | `"ebb752b5…8544"` |
| `versions.agent_d_model` | string | `briefing config tone.llm_model` | `"gpt-4.1-mini"` |
| `operation_log` | list sorted by `at`, then `action` | this report's rows in `operation_log` (database timestamps), plus `plan_confirmed` | `[{"at": "…", "action": "questionnaire_submitted"}, {"action": "report_generated"}, {"action": "report_viewed"}, {"action": "plan_confirmed"}]` |

Never in the record: user id, email, username, IBKR trades or tokens, the questionnaire **score** (only the
C-level leaves the risk profile, as CLAUDE.md decides), free text, news headlines.

## 3. Canonical bytes, commitment, transaction

### 3.1 Canonicalization (same bytes in Python and in the browser)

1. **No floats anywhere.** Weights and other fractions become integer parts per million (`round(x * 1e6)`,
   half-even). Money becomes integer cents. Any other number becomes a decimal **string** with exactly six
   places (`format(x, ".6f")`). The builder raises on a float, NaN or Infinity. This removes the risk that two
   languages print the same number differently (Agent A's values look like `0.22000000000000006`).
2. Keys are ASCII. Objects are serialized with keys sorted by code point. Lists keep the order given above
   (allocation by ticker, checks by name, log by time).
3. `json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`,
   encoded UTF-8, with no trailing newline. The browser uses a 20-line canonical serializer with the same rules,
   and a shared golden test file keeps the two identical.
4. **Store the canonical text, not jsonb.** Postgres jsonb reorders keys and normalizes numbers, so the record is
   stored as the exact canonical string (`record_canonical text`).

### 3.2 Commitment

```
salt        = 32 random bytes (secrets.token_bytes(32)), one per record
commitment  = SHA-256( salt || canonical_record_bytes )          # 32 bytes
```

The salt stops anyone from confirming a guess ("is this C3 with 30% BIL?") by hashing candidates. Without the
salt the fingerprint says nothing. Deleting the salt (the user deletes their data) makes the on-chain value
permanently unlinkable to anyone.

### 3.3 Transaction (Sepolia, chain id 11155111)

- EIP-1559 (type 2) transaction from our sealing address **to the same address**, value 0.
- `data` = ASCII `EGOQ1` (`0x45474f5131`) followed by the 32-byte commitment: 37 bytes.
- Gas limit 30 000. The intrinsic cost is 21 000 + 16 per non-zero byte + 4 per zero byte; it is 21 592 in the
  example. At today's Sepolia fees a seal costs a tiny fraction of a test ETH.
- The transaction hash is the receipt. It is computed locally when the transaction is signed, **before** it is
  sent.

### 3.4 Worked example (fake record, fake salt)

Record (the fields of section 2, fake values) in canonical form, 1 089 bytes:

```
{"allocation":[{"ticker":"BIL","weight_ppm":300000},{"ticker":"SHY","weight_ppm":300000},{"ticker":"VT","weight_ppm":400000}],"confirmed_at":"2026-10-20T08:15:30Z","consent_version":"2026-10-20","investor":{"answers":{"amount_invested_cents":1000000,"amount_needed_cents":0,"choices":{"G1":"b","G2":"c"},"multi_choices":{}},"questionnaire_version":"qv-test","risk_class":"C3"},"operation_log":[{"action":"questionnaire_submitted","at":"2026-10-20T08:10:02Z"},{"action":"report_generated","at":"2026-10-20T08:10:09Z"},{"action":"report_viewed","at":"2026-10-20T08:12:44Z"},{"action":"plan_confirmed","at":"2026-10-20T08:15:30Z"}],"record_version":1,"report_id":"00000000-0000-4000-8000-000000000001","risk_engine":{"approved":true,"checks":[{"direction":"max","limit":"0.400000","name":"Share in stocks","ok":true,"value":"0.400000"}],"violations":[]},"schema":"egoq-record-1","versions":{"agent_d_model":"gpt-4.1-mini","agent_d_prompt_sha256":"ebb752b547507ffcf8edd43fa8eac0bb4dd9990249355a86bbe2bb50ea5c8544","engine":"0.3.0","input_hash":"db076bb630a5b957","risk_profile_config":"test"}}
```

| | Value |
|---|---|
| salt (fake: bytes 0..31) | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| SHA-256(canonical) without salt (for the golden test only) | `cf782b6920b49cbf862bf49ee0e04240d9c342594ab5a1b7a943f683402bf008` |
| commitment = SHA-256(salt ‖ canonical) | `fae178cabdd0e04269b665c8e57bb7ea3d2f906e2e8c195071ab377969b8c2e9` |
| tx `data` | `0x45474f5131fae178cabdd0e04269b665c8e57bb7ea3d2f906e2e8c195071ab377969b8c2e9` |

These values become the first test vector (`tests/onchain/vectors.json`), checked in both Python and the browser.

### 3.5 Proof file (what the investor downloads)

```json
{"proof": "egoq-proof-1", "record": "<the canonical text, exactly>", "salt": "<64 hex>",
 "commitment": "<64 hex>", "chain_id": 11155111, "tx_hash": "0x…", "sealing_address": "0x…"}
```

## 4. Endpoints

All owner endpoints reuse the accounts helpers (`_signed_in`, `_own`, `_call`, `log_action` from
`egoquant/accounts/routes.py`, imported, not edited). Who the user is comes only from their Supabase token.
The routes live in a new router `egoquant/onchain/routes.py`, included from `main.py` with one line.

### 4.1 `POST /account/reports/{report_id}/confirm` (owner): "I confirm this plan"

Request: `{"consent_version": "2026-10-20"}` (must equal `CONSENT_VERSION`, else 409, as `/account/consent` does).
Steps:

1. Load the report (404 if it isn't the user's). 409 if it isn't approved (`summary.approved` false or
   violations present): **rejected portfolios are never confirmed or sealed**. 409 if it was already
   confirmed: the existing confirmation is returned, which makes the call idempotent.
2. Write the consent row (`scope = record_seal`) and the `plan_confirmed` log row.
3. Build the record (section 2), canonicalize it, draw the salt, compute the commitment, and insert the
   `advisory_records` row.
4. Try to seal (section 8) within a **6-second budget**, then answer either way.

Response `200`:
```json
{"confirmed": true, "record_id": "…", "commitment": "…",
 "seal": {"status": "submitted", "tx_hash": "0x…", "chain_id": 11155111,
          "explorer_url": "https://sepolia.etherscan.io/tx/0x…"}}
```
`seal.status` is one of `pending` (not sent yet; it will be retried), `submitted` (sent, waiting for a block),
`confirmed` (in a block) or `failed` (the chain rejected it; retried, see section 8). The plan is confirmed in
every case.

### 4.2 `GET /account/reports/{report_id}/seal` (owner)

Returns the latest seal status. If it is `pending`, this call retries the send; if it is `submitted`, it asks the
RPC for the receipt and records `confirmed` with the block number and block time. This "lazy" update runs with
the owner's token (see section 8: the backend has no service key and so cannot write in the background).
Response: `{"status", "tx_hash", "block_number", "block_time", "explorer_url", "commitment"}`.

### 4.3 `GET /account/reports/{report_id}/proof` (owner)

The proof file (section 3.5) as a download. The user can only get their own.

### 4.4 Change to `POST /account/portfolio` (Onur's file, needs his OK)

- Save the request's `answers` with the report (a new key `inputs.answers` in the saved JSON, without the
  score). The record must use the answers the report was really built from, not a re-sent copy.
- Log `questionnaire_submitted` (before building) next to the existing `report_generated`.
- Add `POST /account/reports/{report_id}/viewed`, called once by the frontend when the report is shown; it logs
  `report_viewed`.

### 4.5 `POST /verify` (public, nothing stored, nothing logged)

Request: the proof file. The server recomputes SHA-256(salt ‖ record) and checks the result against
`commitment`. It reads the transaction by hash from Sepolia and checks:

1. it exists and succeeded;
2. it is on chain 11155111;
3. `from` and `to` are both our published sealing address;
4. `data` starts with `EGOQ1`;
5. the next 32 bytes equal the recomputed commitment.

Response:
```json
{"match": true, "block_number": 1234567, "block_time": "2026-10-20T08:15:42Z",
 "checks": {"hash": true, "tx_found": true, "prefix": true, "sender": true, "commitment": true}}
```
On a mismatch, `match` is `false` and `checks` shows which failed: "hash differs", "tx not found", "wrong prefix"
or "not sent by EgoQuant". Rate-limited per IP (20 a minute). The proof file is not logged. **Recommended
default:** the `/verify` page does steps 1 and 5 in the browser (WebCrypto) and asks the backend only for the
transaction by hash (`GET /verify/tx/{tx_hash}`). That way the record, which contains the investor's answers,
never leaves their device. `POST /verify` remains for the CLI and for judges.

## 5. Storage: `supabase/migrations/003_onchain.sql` (written by Onur's Supabase agent)

Same model as 001/002:
- Row-Level Security (`user_id = auth.uid()`) for select and insert.
- No anonymous access, and column-level insert grants without the timestamp or expiry columns.
- 180-day `expires_at`, and deletion through `delete_my_data` / `delete_my_account`.
- **Insert-only, no UPDATE grants:** each status change is a new row, so the user can't rewrite a seal row
  through PostgREST.

```sql
create table if not exists public.advisory_records (
    id                uuid primary key default gen_random_uuid(),
    user_id           uuid not null default auth.uid() references auth.users (id) on delete cascade,
    report_id         uuid not null references public.reports (id) on delete cascade,
    record_version    integer not null default 1 check (record_version between 1 and 100),
    schema            text not null check (schema = 'egoq-record-1'),
    record_canonical  text not null check (length(record_canonical) < 65536),
    salt_hex          text not null check (salt_hex ~ '^[0-9a-f]{64}$'),
    commitment_hex    text not null unique check (commitment_hex ~ '^[0-9a-f]{64}$'),
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null default now() + interval '180 days',
    unique (report_id, record_version)                 -- one seal per confirmed report version
);
create table if not exists public.record_seals (           -- append-only: the latest row is the status
    id            bigserial primary key,
    user_id       uuid not null default auth.uid() references auth.users (id) on delete cascade,
    record_id     uuid not null references public.advisory_records (id) on delete cascade,
    at            timestamptz not null default now(),
    status        text not null check (status in ('pending', 'submitted', 'confirmed', 'failed')),
    chain_id      bigint not null check (chain_id in (11155111)),
    tx_hash       text check (tx_hash ~ '^0x[0-9a-f]{64}$'),
    block_number  bigint,
    block_time    timestamptz,
    error_kind    text check (length(error_kind) <= 32),   -- 'rpc_down', 'nonce', 'rejected' (never a message)
    expires_at    timestamptz not null default now() + interval '180 days'
);
```

The same file also:
- Turns on RLS and creates the `own_rows_select` / `own_rows_insert` policies, using the loop pattern from 002.
- Revokes everything, then grants `select` and column-level `insert` to `authenticated`.
- Widens `operation_log.action` with `questionnaire_submitted`, `report_viewed`, `plan_confirmed`,
  `record_sealed` and `seal_pending`.
- Widens `consents.scope` with `record_seal`.
- Adds both tables to `delete_my_data()` and `purge_expired()` (`create or replace`). These are Onur's
  functions, so his agent edits them.
- Extends `supabase/tests/rls_isolation.sql`: user B can't see A's records or seals, anonymous users see none,
  and an update is refused.

## 6. Frontend (Next.js, `fintechaton-frontend`)

- **Confirm button** on the portfolio page (`src/app/portfolio/page.tsx`, Onur's). It shows only when the
  user is signed in, the Risk Engine approved the portfolio and the report was saved (`report_id`).
  - A visitor sees "Sign in to confirm and seal this plan".
  - A rejected portfolio shows no button.
  - The dialog says in plain words: "We will keep this record and publish its fingerprint (no personal data)
    on a public test blockchain. You can download a proof file." It has an agree box that writes the
    `record_seal` consent.
- **Seal badge** (new `src/components/seal-badge.tsx`):
  - "Sealed on Sepolia ✓ · block N · 20 Oct 2026 · View on Etherscan ↗";
  - "Sealing… (waiting for a block)";
  - "Seal pending · Retry". This never hides or delays the plan.
  - The badge polls `GET …/seal` every 5 s for up to 2 minutes, then stops (a page reload checks again).
- **Proof download:** the "Download proof file" button saves `egoquant-proof-<report_id>.json`.
- **`/verify` page** (new `src/app/verify/page.tsx`, public, linked from the footer):
  - Upload a proof file. The page canonicalizes and hashes it in the browser, fetches the transaction through
    `/api/verify/tx/<hash>`, and shows "Unchanged ✓ — sealed in block N on <date>" or "Does not match ✗"
    with the failing check.
  - A "Try a tampered copy" button changes one weight in the loaded file and runs the check again, for the
    demo.
- **Proxy routes**, in the existing `server-auth` pattern: `src/app/api/account/reports/[id]/{confirm,seal,proof,viewed}`
  and `src/app/api/verify/tx/[hash]`.
- **Types:** `src/lib/egoquant.ts` (shared), with `SealStatus` and `ProofFile`.
- The account page (Onur's) shows the seal badge on each saved report (optional).

## 7. Security

- **Private key:** only in the environment (`EGOQ_SEAL_PRIVATE_KEY`, Railway secret and local `.env`). It is
  read once into an `eth_account` account object and never logged, printed, returned, put in an exception
  message or committed.
  - The module exposes the **address** only.
  - A test sets a known fake key, runs confirm, seal, failure and retry, and asserts the key's hex (with and
    without `0x`) appears in no log record and no response.
  - Exceptions from the signing library are caught and replaced by `error_kind` labels.
- **Dedicated wallet:** a new key used only for sealing, holding only Sepolia test ETH. It is never reused
  elsewhere, never on mainnet, and is rotated if exposed. The address is public (shown on `/verify` and in
  `README.md`).
- **Rate limits:**
  - at most 5 seals per user per day (`EGOQ_SEAL_MAX_PER_USER_DAY`) and 200 a day in total
    (`EGOQ_SEAL_MAX_PER_DAY`);
  - `/verify` at 20 a minute per IP;
  - the existing `/account` limit (30 a minute per user) also applies;
  - over a cap, the plan is still confirmed and the seal stays `pending`.
- **No personal data on-chain:** only `EGOQ1` plus 32 bytes. No user id, email, address, amount or ticker.
- **Salt:** 32 random bytes per record, so nobody can test guesses against the fingerprint.
- **Replay and duplicates:**
  - One record per `(report_id, record_version)` (unique constraint), and the commitment is unique.
  - A second confirm returns the existing record.
  - A pending seal is re-sent with the **same** commitment, never a new salt.
  - The nonce is taken under a process lock. A send that may or may not have gone through is checked by tx hash
    before a new send.
- **Who can see what:** a record, salt or proof is visible only to its owner (RLS plus the backend's
  `user_id` filter). `/verify` stores nothing.
- `SECURITY.md` gets a section (Onur's file, through the coordination file).

## 8. Failure handling, retries, idempotency

- **Sealing never blocks advice.** The confirm call writes the record first, then tries to seal within
  6 seconds, and answers either way.
- **Sending:**
  1. Read the nonce (`eth_getTransactionCount(addr, "pending")`) and the fees (`eth_maxPriorityFeePerGas` and
     the base fee from `eth_feeHistory`; max fee = 2 × base + tip).
  2. Sign locally; the **tx hash is known now**.
  3. Insert a `submitted` seal row with that hash, then `eth_sendRawTransaction`.
  4. If the send errors, query `eth_getTransactionByHash`. If found, keep `submitted`; if not, insert
    `pending` with `error_kind = rpc_down`.
- **RPC down:** try each URL in `EGOQ_SEAL_RPC_URLS` in turn (public endpoints that answered today:
  `https://ethereum-sepolia-rpc.publicnode.com`, `https://sepolia.drpc.org`, `https://1rpc.io/sepolia`).
  If all fail, the seal stays `pending`.
- **Retries without a service key:** the backend writes to Supabase only with the user's token (Onur's design).
  So retries run when the owner is present:
  - when the seal badge polls `GET …/seal`;
  - when the user opens the report again;
  - through the "Retry" button;
  - through the CLI (section 12) run by us for our demo records.
  - Each retry re-sends the **same** commitment.
- **Confirmation:** a `submitted` seal becomes `confirmed` once the receipt shows status 1. If the receipt is
  missing after 30 minutes and the tx is unknown to the node, the seal is re-sent with a new nonce, the same
  data and a higher fee.
- **Idempotency:** confirm (unique record), seal (same commitment, existing tx hash checked first), verify
  (read-only).

## 9. Tests (CI never touches the chain)

All in `tests/onchain/`, with a fake RPC (an in-memory chain: nonces, a mempool, receipts) injected the same way
the accounts tests inject a fake store.

1. **Canonicalization golden test:** the record of section 3.4 gives exactly those 1 089 bytes. Floats, NaN and
   non-ASCII keys raise. Key order and whitespace in the input don't change the bytes.
2. **Commitment test vectors** (`vectors.json`), with at least 3 records, run in Python and through the
   browser serializer with Node, so the two give identical bytes and hashes.
3. **Tx data encoding:** 37 bytes, `EGOQ1` prefix, gas 21 592 for the vector, chain id 11155111, to = from.
4. **Verify:** a match, plus each mismatch kind (hash differs, tx not found, wrong prefix, wrong sender, wrong
   chain).
5. **Tamper test:** change one weight by 1 ppm, one answer or one log time, and the verdict is "Does not match"
   with `hash differs`.
6. **The key never leaks:** a known fake key, the full confirm, failure and retry flow, and caplog plus every
   response body scanned for the key hex.
7. **Sealing failure doesn't block confirmation:** with the RPC down, confirm still returns 200 with
   `confirmed: true` and `seal.status: pending`, and a later `GET …/seal` with the RPC back gives `submitted`,
   then `confirmed`.
8. **Rules:** a rejected report gives 409 with nothing stored or sent; a second confirm returns the same
   record; another user's report gives 404; the per-user and global caps apply.
9. **No personal data on-chain:** the only bytes sent are prefix plus commitment, and the record has no
   user id or email.
10. **Live smoke test** (`-m live`, not in CI): one real Sepolia self-tx using the local `.env` key.

## 10. Environment (Railway; names only)

| Name | Meaning |
|---|---|
| `EGOQ_SEAL_ENABLED` | `1` to seal; anything else means confirm still works and seals stay `pending` |
| `EGOQ_SEAL_PRIVATE_KEY` | the sealing wallet's key (Railway **secret**; never in git, chat or logs) |
| `EGOQ_SEAL_RPC_URLS` | comma-separated Sepolia RPC URLs, tried in order |
| `EGOQ_SEAL_CHAIN_ID` | `11155111` |
| `EGOQ_SEAL_EXPLORER` | `https://sepolia.etherscan.io` |
| `EGOQ_SEAL_MAX_PER_USER_DAY` / `EGOQ_SEAL_MAX_PER_DAY` | caps (5 / 200) |

One-time wallet setup (details in the recipe):
1. A script creates a new key, writes it into the backend `.env` and prints **only** the address.
2. Fund the address from a Sepolia faucet; 0.05 test ETH covers thousands of seals.
3. Give the key to Umut privately for the Railway secret.
4. Check the balance with `python -m egoquant.onchain.seal --balance`.

Library: **`eth-account`** (0.14.0) for key handling and EIP-1559 signing, plus plain JSON-RPC over
`requests`, which is already a dependency. On 19 Oct it installed and signed on Python 3.14, has Linux
Python 3.14 wheels for all its native parts, and kept every pinned version. `web3.py` is not needed; it adds
aiohttp and more for nothing we use. New line in `requirements.txt`: `eth-account==0.14.0` (Umut's deploy file).

## 11. Evidence for the submission

- 3–5 real sealed records from different investors (for example C1 gate, C3 balanced, C5, and one large
  portfolio). For each: the report id, commitment, tx hash, block, and Etherscan link
  `https://sepolia.etherscan.io/tx/<hash>`. Collected in `docs/ON_CHAIN_EVIDENCE.md`.
- Screenshots:
  - the confirm dialog;
  - the "Sealed on Sepolia ✓" badge;
  - the Etherscan page showing the `EGOQ1…` input data;
  - `/verify` with "Unchanged ✓";
  - `/verify` with a tampered copy showing "Does not match ✗ (hash differs)".
- One proof file for a judge to verify themselves (a fake demo investor only).

## 12. Fallback if the live deploy isn't ready

`python -m egoquant.onchain.seal`, run from the Mac with the key in the local backend `.env`:
- `seal <record.json>`: canonicalizes a confirmed record (exported from a locally confirmed report, via the
  launcher in live mode with the `onchain` branch), draws the salt, signs and sends, waits for the receipt, and
  writes `<name>.proof.json`. These are real Sepolia hashes.
- `verify <proof.json>`: the same checks as `POST /verify`.
- `retry`: re-sends this machine's pending proofs.
- `--balance`: shows the sealing address and its test-ETH balance.

## 13. Time plan (Abu Dhabi time, UTC+4)

| When | What |
|---|---|
| Mon 19 Oct evening | Wallet created and funded (Bartu). Branch `onchain`: record, canonicalization, commitment, chain client with fake RPC, tests 1–9. Migration 003 written by Onur's Supabase agent. |
| Mon night | Confirm, seal, proof and verify routes; frontend button, badge, proof download and `/verify`; one live smoke seal; merge to oct19 when tests pass. |
| Tue 20 Oct by 10:00 | Railway variables set (Umut); 003 run in Supabase (Onur); deploy checked. If not live by 11:00, use the CLI fallback. |
| Tue by 14:00 | 3–5 real seals, screenshots, `docs/ON_CHAIN_EVIDENCE.md`. |
| Tue 19:59 | Submission deadline (23:59 Beijing). |

## 14. Who touches what

| Part | Files | Owner | Note |
|---|---|---|---|
| Record, canonical form, commitment, chain client, CLI | `egoquant/onchain/*` (new) | on-chain agent | new files only |
| Confirm, seal, proof, verify routes | `egoquant/onchain/routes.py` (new) | on-chain agent | imports the accounts helpers, doesn't edit them |
| Router include | `main.py` | shared (eylul, Onur, Umut) | **one line; ask first** |
| Save answers, log `questionnaire_submitted`, the `viewed` route | `egoquant_routes.py` | Onur (Ryukazaki), Damla | **teammate's file** |
| New log actions, consent scope | DB constraints in 003; `routes.py` is unchanged | Onur | through the migration |
| Migration 003 and RLS test | `supabase/migrations/003_onchain.sql`, `supabase/tests/rls_isolation.sql` | Onur's Supabase agent | **teammate's files** |
| `delete_my_data` / `purge_expired` | redefined in 003 | Onur | **teammate's functions** |
| Security notes | `docs/SECURITY.md` | Onur | **teammate's file** |
| Dependency | `requirements.txt` | Umut (deploy), Onur | **one line; ask first** |
| Portfolio page button, account page badge | `src/app/portfolio/page.tsx`, `src/app/account/page.tsx` | Onur, Damla, Umut | **teammates' files** |
| Badge, `/verify` page, proxies | new files in the frontend | on-chain agent | new files |
| Types | `src/lib/egoquant.ts` | shared | additive only |
| Agent D, `report.py` What changed | | Bartu | not touched |
| Regime, what-if | `egoquant/regime*`, `what_if*`, `tests/regime/*` | Zerya, Damla | not touched |
