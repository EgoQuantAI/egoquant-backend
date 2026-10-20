# On-chain decisions (Aslan)

Savas's rule (20 Oct 2026, about 08:40 Dubai): **Aslan's recorded on-chain decisions win over every session**, as Bideci's
recorded UI decisions (`UI_DECISIONS.md`, frontend repo) win for the UI. Savas can overrule. Every session follows the lines
marked `binding`. A `proposal` is Aslan's AI's suggestion or default: it is not Aslan's decision until he confirms it.

Guard rails that are not taste (Savas, same day): every existing proof file keeps verifying (new formats are versioned and
additive), keys live only in Railway's environment, testnet only (chain id 11155111) unless Aslan **and** Savas decide
otherwise in writing, and advice is never blocked when the chain or the RPC is down (the plan is confirmed and the seal is sent later; the badge reads "Sealing… (the fingerprint will be sent shortly; your plan is confirmed)").

Kept by Aslan's AI on branch `docs/aslan-N`. One line per decision; a later line can replace an earlier one by naming its ID.

| ID | Decision (plain words) | Date | Source | Status |
|---|---|---|---|---|
| OC-01 | Only a salted 32-byte fingerprint goes on the chain, never personal data: `SHA-256(salt ‖ canonical record)` in a 0-ETH transaction from EgoQuant's sealing address to itself, input `EGOQ1` + the 32 bytes, on Sepolia. | before 19 Oct 2026 | Aslan's kickoff hard rules ("only a salted 32-byte digest goes on-chain") | binding |
| OC-02 | The sealing key `EGOQ_SEAL_PRIVATE_KEY` exists only in Railway's environment. No key, token or mnemonic in a file, commit, log, message or chat; use variable names only; the key is never printed. | before 19 Oct 2026 | Aslan's kickoff hard rules | binding |
| OC-03 | Tests never use the network or a real key (fake chain `FakeAnchorClient`, fake store). | before 19 Oct 2026 | Aslan's kickoff hard rules | binding |
| OC-04 | The seal consent has its own version (`SEAL_CONSENT_VERSION`), separate from the IBKR import consent. | before 19 Oct 2026 | Aslan's decision message ("separate SEAL_CONSENT_VERSION") | binding |
| OC-05 | A sealed report is frozen: once confirmed it is never rebuilt or overwritten. | before 19 Oct 2026 | Aslan's decision message ("freezing sealed reports yes") | binding |
| OC-06 | `/verify` sits in the 4.5 MB request-size group (a sealed report with its document fits). | before 19 Oct 2026 | Aslan's decision message | binding |
| OC-07 | No key or token value, nor any fingerprint of one, in `/data/status` or any public endpoint (`"key": "set"` / `"not set"` only), with a test (`tests/test_no_secrets_in_public_endpoints.py`). | 19 Oct 2026 | Aslan's final-round brief, Part 1(b) | binding |
| OC-08 | One final `delete_my_data()` / `purge_expired()` that covers every user table, in `007_erase_everything.sql`, run last. | 19 Oct 2026 | Aslan's final-round brief, Part 1(a) | binding |
| OC-09 | **Seal v2 is the default for new seals.** The final report is rendered once at confirm into the *sealed document* (its header says "Sealed for: <username> · report <id8>") and stored byte-exact. Record `egoq-record-2` adds `versions.report_document_sha256` and `client = {username, report_id}` (never an email or a user id). Seal file `egoq-proof-2` includes the document. The chain transaction is unchanged (OC-01). It defeats (A) a genuine proof shown next to an edited report and (B) someone else's genuine report. | 19 Oct 2026 | Aslan's final-round brief, Part 2 | binding |
| OC-10 | The download is ONE sealed report: the document, then a footer between `<!--EGOQ-SEAL-FOOTER-BEGIN-->` and `<!--EGOQ-SEAL-FOOTER-END-->` with the visible Seal section and a hidden `<script type="application/json" id="egoq-proof">`. Button: **Download sealed report**, plus a small **Technical seal file (.json)** with a tooltip. | 19 Oct 2026 | Aslan's final-round brief, Parts 2 and 3 | binding |
| OC-11 | `/verify` accepts the sealed report (.html) or the seal file (.json) and hashes **in the browser**. Success: "Unchanged ✓ · Sealed for: <username> · confirmed <date> · block N", then the original sealed document (PDF via print, Etherscan link). A report without a seal: "This report was never sealed." | 19 Oct 2026 | Aslan's final-round brief, Part 2 | binding |
| OC-12 | Old seals keep verifying: `egoq-record-1` / `egoq-proof-1` verify with the note "older seal format, no document fingerprint". | 19 Oct 2026 | Aslan's brief (v2 must not break v1) + Savas's guard rail | binding |
| OC-13 | **My seals** on the Account page lists the user's own seals with their transactions. | 19 Oct 2026 | Aslan's final-round brief, Part 2 | binding |
| OC-14 | UI: the seal wording sits before the button; sidebar item **Verify a report** (shield icon, subline "Check a sealed report on the blockchain"); `/verify` in 3 steps, with the demo report and a link to the sealing address. Account: Saved reports and My seals first, IBKR second as "Optional: learn from your past trades (Interactive Brokers)" with a sample file. A note says a PDF copy cannot prove itself. Bideci's UI decisions still apply (no new colours, red only for problems). | 19 Oct 2026 | Aslan's final-round brief, Part 3 | binding |
| OC-15 | The signed-in check "This report is from your account / belongs to a different account" on `/verify` is **not built** this round (only if everything else was finished and pushed). | 19 Oct 2026 | Aslan's final-round brief, scope note | binding (deferred) |
| OC-16 | The seal waits until the report's news rating is finished (`409` "news not final"), so the fingerprint covers the finished report. | 19 Oct 2026 | Aslan's brief ("news fully rated in every report", on-chain steps "complete and clear"); the implementation choice is Aslan's AI's | proposal |
| OC-17 | The seal v2 column lives in its own migration `008_seal_v2.sql` (007 stays as applied). Until 008 runs, confirm falls back to a v1 seal instead of failing (`test_without_migration_008_the_plan_is_still_confirmed_and_sealed_as_v1`). | 20 Oct 2026 | Aslan's AI, after Savas's brief (007 already applied live) | proposal |
| OC-18 | Limits: 5 seals per account per day, 200 a day in total (`EGOQ_SEAL_MAX_PER_DAY`, marked provisional in `egoquant/anchoring/config.py`); past them the plan is still confirmed and the seal waits. | before 19 Oct 2026 | implemented by Aslan's AI; Aslan has not confirmed the numbers | proposal |
| OC-19 | Roadmap, not built: the same commitment on a consortium chain such as FISCO BCOS (what changes: the client and the chain id; what stays: the record, the salt, the commitment, the verifier). | 20 Oct 2026 | Savas's brief (open facts) | proposal |

## Bonus points (on-chain work)

The competition's exact bonus criteria for on-chain work were not available to the team, so none is claimed here. What is built for the on-chain work: OC-01 to OC-14 (a real public seal on Sepolia that anyone can check without an account,
with the report itself, its owner's username and a tamper check in the browser).
