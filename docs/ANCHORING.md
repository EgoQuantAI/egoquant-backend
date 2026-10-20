# On-chain sealing of confirmed advice (`egoquant/anchoring`)

Status: built, tested and live (seal v2), 20 Oct 2026. Real Sepolia seals of real engine
output on real market data (invented investors) are listed in `docs/ON_CHAIN_EVIDENCE.md`. The wiring into `main.py`, the portfolio page and Supabase is done; the decisions are in
`docs/ONCHAIN_DECISIONS.md`. **Off by default:** without `EGOQ_SEAL_ENABLED=1`, a key and RPC URLs, the
feature is hidden.

## What it does, in plain words

When a signed-in investor confirms an **approved** plan, EgoQuant writes down exactly what it advised: the risk
class and questionnaire answers, the allocation, the Risk Engine's checks, the versions of the code and models, and
the steps the investor took (questionnaire submitted, report generated, viewed, confirmed). It also takes a
fingerprint (SHA-256) of the whole saved report the investor saw. It then publishes a **32-byte fingerprint** of
that record on a public test blockchain (Ethereum Sepolia). The investor can download a **proof file**. If anyone,
including us, later changes one weight, one answer or one log entry, the fingerprint no longer matches the one fixed
in a public block on a known date. That is tamper-evidence for advice.

We **fingerprint** (hash) the record and the report. We never encrypt them and never put them on-chain.

## How

| Step | Code |
|---|---|
| Record `egoq-record-1` from the saved report (`inputs.answers`, allocation in ppm summing to 1 000 000, checks, versions, `versions.report_sha256`, this report's log rows) | `record.py` (`build_record`) |
| Canonical JSON: sorted keys (UTF-16 order), compact, UTF-8. No floats in the record. In the report, integral numbers are written as integers and other numbers as 6-decimal strings (exact, ties to even). | `canonical.py`; the browser twin is `src/lib/proof.ts` |
| `commitment = SHA-256(salt ‖ record)` with a fresh 32-byte salt per record | `commit.py` |
| A 0-ETH EIP-1559 transaction from the sealing address **to itself**, data = `EGOQ1` + commitment (37 bytes, 21 592 gas), chain 11155111 only | `client.py` (`SepoliaAnchorClient`) |
| Proof file `egoq-proof-1` (record, salt, commitment, tx hash, sealing address, saved report) and its verification | `proof.py` |
| Routes: `/anchoring/status`, confirm, seal, proof, viewed, `/verify`, `/verify/tx/{hash}` | `routes.py` |
| CLI fallback: `python -m egoquant.anchoring --balance \| demo-record \| seal \| verify \| tamper \| retry` | `__main__.py` |
| Wallet creation (prints only the address) | `scripts/onchain_new_wallet.py` |

The record never holds a user id, an email, IBKR data, the questionnaire score, free text or headlines. The v1 record (above) holds no username either; the v2 record (below) holds the person's chosen username, and nothing else that identifies them.

## Safety rules (and where they are enforced)

- **The key** (`EGOQ_SEAL_PRIVATE_KEY`) is read only from the environment, into an `eth_account` object. It is
  never logged, printed, returned or put in an error. Only the address is exposed. Tests use a key made at run
  time and scan every log, response, repr and store row for it (`test_client.py`, `test_routes.py`).
- **Testnet only:** any chain id other than 11155111 is refused. Budget: $0 (public RPCs, free test ETH).
- **Never blocks or changes advice:** confirm stores the record first, then tries to seal within 6 s
  (`EGOQ_SEAL_BUDGET_S`) and answers either way. If the chain is down the seal stays `pending` and is re-sent (with
  the **same** commitment) when the owner's seal badge polls. Only approved reports with no violations are
  confirmed (409 otherwise, with nothing stored or sent). No number, limit or verdict is touched.
- **Only the final report is sealed:** the Confirm button waits until Agent D's final news briefing is saved, and
  006 makes `replace_report_briefing` refuse a sealed report.
- **Caps:** 5 seals per user per day, 200 a day per process. Over a cap the plan is still confirmed, and the seal
  waits.
- **`/verify`** stores and logs nothing. It checks the hash with a strict regex, spends at most 5 s on the chain,
  and is limited to 20 calls a minute per IP plus 120 a minute in total. The `/verify` page hashes the proof **in the
  browser**, and only the transaction hash reaches our server.
- **Idempotent:** one record per (report, version) and a unique commitment. A second confirm returns the same
  record. A send that timed out is looked up by its hash before anything is sent again.

## Honest limits

- **A public blockchain entry can never be deleted.** What is on-chain is only `EGOQ1` plus a salted 32-byte
  fingerprint. Without the record **and** its salt, the fingerprint reveals nothing and can't be tested against
  guesses. "Delete my data" (`delete_my_data()`, and report or account deletion by cascade) deletes the record
  and its salt, so we can **no longer link** the fingerprint left on-chain to the investor or to anything
  else. The consent text says this before the investor agrees.
- The answers, amounts included, sit in `advisory_records` in clear text. Only the owner can read them (RLS), and
  they are kept for 180 days. The proof file holds them too, and the investor keeps it.
- A seal proves that a record existed, unchanged, at a time. It does **not** prove that the advice was right.
- `record_seals` rows can be added by their owner through the database API, for example a fake "confirmed".
  That only misleads the owner, because `/verify` reads the chain, never the table.
- Sepolia is a public **test** network. In production the same fingerprint format would go to a consortium chain
  such as WeBank's FISCO BCOS (future work).
- Retries need the owner present (no service key, by design). A seal can stay `pending` until the investor
  opens the report again, or until we run `retry` from the CLI.

## Evidence

See `docs/ON_CHAIN_EVIDENCE.md`. In short:
- **Part A:** five seals of real EgoQuant reports, built by the deployed engine on real historical prices (as of
  16 Oct 2026) for the team's invented demo personas (C1, C3, C4, C5). One was confirmed by clicking through the
  real portfolio page. All verify "Unchanged ✓", and every tampered copy says "Does not match ✗ (hash differs)".
- **Part B:** the earlier CLI smoke tests, sealed from hand-written test records (not engine output).

Check it yourself:
```bash
python -m egoquant.anchoring verify docs/anchoring/real-balanced_saver.proof.json
```

## Seal v2: the sealed report (20 Oct 2026)

**Two attacks a document seal must defeat.**
- **(A) A genuine proof file + an edited report.** Someone keeps a real proof, edits a weight or a sentence in the report
  next to it, and shows both. If only the record (answers, weights in ppm, checks) were sealed, the record would still
  verify, and the reader might wrongly trust the edited report.
- **(B) Someone else's genuine report + proof.** Everything verifies, because it really was sealed, just not for the person
  showing it. The reader might wrongly think it is theirs.

**How v2 stops them.**
- At confirm, after the news is final, the final client report is rendered **once** into the **sealed document**. It is
  stored byte-exact with its record (owner-only, insert-only, erased by "Delete my data") and never rendered again. Its
  own header says **"Sealed for: <username> · report <id>"**.
- The record (`egoq-record-2`) adds `versions.report_document_sha256`, the SHA-256 of the document's exact UTF-8 bytes,
  and `client = {username, report_id}` (the username only, never an email or a user id). The chain still gets only
  `EGOQ1` + the 32-byte commitment of the salted record.
- The download is the **sealed report**: the document, then a footer between `<!--EGOQ-SEAL-FOOTER-BEGIN-->` and
  `…END-->` with the visible Seal section and a hidden `<script type="application/json" id="egoq-proof">` (record, salt,
  commitment, transaction). The **technical seal file** (`.json`) carries the same plus the document itself.
- `/verify` takes either file and checks it **in the browser**. First, SHA-256 of the document part (everything before
  the last footer marker, or the `.json`'s document) must equal `report_document_sha256`; otherwise the result is
  "Does not match ✗: this report was changed after it was sealed" **(A)**. Then SHA-256(salt ‖ record) must equal the
  commitment in the Sepolia transaction, sent by our address with the `EGOQ1` prefix; otherwise "the seal doesn't match
  the blockchain". That catches a forged footer whose record was rewritten to fit an edited document, because only the
  original commitment is on the chain.
- On success it shows **"Unchanged ✓ · Sealed for: <username> · confirmed <date> · block N"** and then the original
  sealed document. It is rendered from the verified bytes in a sandboxed frame, never from anything outside the
  fingerprint. Because the username is inside the fingerprinted document and the record, **(B)** is visible: a report
  sealed for someone else says so, and changing the name breaks the fingerprint. A signed-in user can also compare the
  transaction with **Account → My seals**.
- v1 seals still verify, marked "older seal format, no document fingerprint". A report that was never sealed says "This
  report was never sealed".
- **Database:** `supabase/migrations/008_seal_v2.sql` adds the `sealed_document` column (and its insert grant) to
  `advisory_records`. Until it is applied, confirm never fails: the database refuses the v2 row, and the plan is sealed
  in the v1 format instead (`test_without_migration_008_the_plan_is_still_confirmed_and_sealed_as_v1`).

**Honest limits.** A seal proves **what** was confirmed, **for whom** (the account's username) and **when**. It does not
prove that the advice was right, and it does not prove who is holding the file. A printed or PDF copy can't prove
itself: only the sealed `.html` or the seal file can be checked. Tests: `tests/anchoring/test_seal_v2.py` (Python) and
`scripts/check-vectors.mjs` (the browser code on a backend-made v2 report).

## How to try the Seal (for judges)

Sealing needs an account, because the record is kept for its owner. About a minute once signed in:

1. **Sign up or sign in** (sidebar → *Sign in · Account*). Sign-up asks for an email, a password and a username.
   If email confirmation is on, open the link in the email, then sign in.
2. **Build a portfolio** while signed in: answer the questionnaire and the investment questions, then open
   *Portfolio*. The report is saved to your account.
3. When the news rating has finished, the button reads **Not sealed · Confirm and seal**. Click it, read the text,
   tick *I have read this and I agree*, and click **Confirm and seal**. Only a plan the Risk Engine approved can be
   confirmed.
4. Usually within a minute the badge changes from *Sealing… (waiting for a block)* to **Sealed on Sepolia ✓ · block N**.
   *View on Etherscan* shows the transaction: 0 ETH, from our sealing address to itself, input `EGOQ1` + 32 bytes.
5. Click **Download sealed report** (`egoquant-sealed-report-<id>.html`).
6. Click **Verify a report** (sidebar), then **Upload a sealed report**. Choose the file: **Unchanged ✓ · Sealed for:
   <you>**, then the original report. Edit any number in the file and upload it again: **Does not match ✗: this report
   was changed after it was sealed**. (*Use the demo report* and *Try a tampered copy* work without an account.)

**Limit:** 5 seals per account per day (and 200 a day in total). Past that the plan is still confirmed, and the badge
shows *Sealing… (the fingerprint will be sent shortly; your plan is confirmed)* until the seal can be sent the next day.

### Backup: three demo accounts (for the team, created by hand)

In case sign-up email is slow or rate-limited during the demo. **No password goes into git, a document or a chat:**
Bartu or Bideci choose them and hand them to the judges directly (for example on paper or in the submission form).

1. Use addresses the team controls, e.g. Gmail plus-addresses of one inbox: `<team-inbox>+demo1@gmail.com`,
   `…+demo2@…`, `…+demo3@…` (all three arrive in the same inbox).
2. Create each one on the **site's sign-up page** with username `demo1`, `demo2`, `demo3`. Don't use the
   dashboard's *Add user* for this: our database trigger takes the username from the sign-up form, and a user
   created without one is refused.
3. Confirm each address: click the link in the inbox (it lands signed out; that's expected), or, if the email
   doesn't come, confirm the user in Supabase → *Authentication → Users* if your dashboard offers it. Otherwise
   switch *Confirm email* off for the minute it takes to sign up (see below) and back on afterwards.
4. Sign in once with each to check it works, then give each judge a different account.
5. Know the limits: everyone using one account shares its 5 seals a day and its saved reports, and *Delete my data*
   on one account deletes them for whoever else uses it. One account per judge avoids this.

Supabase setting, for whoever owns the project (read on 19 Oct, no change made): **Authentication → Providers →
Email → "Confirm email"** is **on**. Switched off, sign-up signs the person in at once (the sign-up route already
handles that), but addresses are no longer verified. Decide for the demo period only, and switch it back on after.

## Tests

`pytest tests/anchoring -m "not live"`: canonical golden bytes (the plan's 1 089-byte example), shared vectors
(also run by the browser code: `node scripts/check-vectors.mjs` in the frontend), the record builder, the Sepolia
client against a fake JSON-RPC node (real signing), verification of every mismatch kind, the CLI, the wallet script,
and the routes with the accounts fake store (full flow, 409s, 404s, RPC down then recovery, caps, the key never
leaks, `/verify` stores nothing). `tests/anchoring/test_live_seal.py` (`-m live`) makes one real seal and needs the
key.

## For SECURITY.md (Onur, W8): suggested section

> **On-chain sealing (`egoquant/anchoring`, off by default).** On confirm, an approved report's record (risk class,
> answers, allocation, Risk Engine checks, versions, a fingerprint of the saved report, the log of the investor's
> steps) is stored for its owner only (RLS, 180 days, insert-only), and `SHA-256(32-byte random salt ‖ canonical
> record)` is published in a 0-ETH Sepolia self-transaction (`EGOQ1` + 32 bytes; no personal data). The signing key
> is a dedicated testnet-only wallet, read only from the environment and never logged or returned (tested). Sealing
> never blocks or changes advice. `/verify` stores nothing, checks in the browser, and is rate-limited. Residual:
> the chain entry cannot be deleted (deleting the data deletes the salt, which makes the fingerprint permanently
> unlinkable); a user can add misleading seal rows for their own record (the chain, not the table, is the proof).
