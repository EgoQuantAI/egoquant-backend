# On-chain evidence: real Sepolia transactions

**What the seal is.** When an investor confirms a plan that EgoQuant's independent Risk Engine approved, EgoQuant takes a fingerprint
of the exact confirmed report and publishes **only that fingerprint** on the public **Ethereum Sepolia test network** (chain id 11155111).
Each seal is a transaction that moves no money (0 ETH), sent by EgoQuant's sealing address to itself, whose input data is `0x45474f5131`
(the letters `EGOQ1`) followed by the 32-byte commitment `SHA-256(salt ‖ record)`. **No personal data goes on the chain**: without the
record and its random salt, the 32 bytes reveal nothing.

**Sealing address (every seal ever made):** [`0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC`](https://sepolia.etherscan.io/address/0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC)

**What is real and what is invented.** The investors are invented demo personas (no real person, account or answers). Everything else in
part A and C1 is real: the portfolio, the Risk Engine's checks and the report were produced by EgoQuant's engine on real historical market
data (prices as of 16 Oct 2026), and sealed through the real confirm route. C2 was made on the **deployed website** by a team member
signed in with a demo account.

## Seals of real engine output (A) and of the sealed report, seal v2 (C)

All rows re-checked on the chain at the time of writing: [verify_snapshot.md](verify_snapshot.md) (12 of 12 match). Machine-readable:
[transactions.csv](transactions.csv).

| ID | What was sealed | Portfolio | Transaction | Block | Block time | Verify | Tampered copy |
|---|---|---|---|---|---|---|---|
| A1 | C1 no-loss saver (invented) | BIL 100.0% | [`0x26e84ac7c5c3317830ca1dfddd791ef96289d4fbdfa071bad1ba1f7cc40e33c5`](https://sepolia.etherscan.io/tx/0x26e84ac7c5c3317830ca1dfddd791ef96289d4fbdfa071bad1ba1f7cc40e33c5) | 11,949,297 | 2026-10-19 16:16:00 UTC | Unchanged ✓ ([proof file](proofs/real-no_loss_saver.proof.json)) | Does not match ✗ ([tampered copy](proofs/real-no_loss_saver.tampered.proof.json)) |
| A2 | C3 balanced saver (invented) | BIL 30.0%, SCHD 16.0%, SHY 30.0%, VIG 19.4%, VTI 4.6% | [`0x50617149a2709deca1c068a9021f3e9b94aafc03f0a5e0d0cd1f41da23b7538c`](https://sepolia.etherscan.io/tx/0x50617149a2709deca1c068a9021f3e9b94aafc03f0a5e0d0cd1f41da23b7538c) | 11,949,299 | 2026-10-19 16:16:24 UTC | Unchanged ✓ ([proof file](proofs/real-balanced_saver.proof.json)) | Does not match ✗ ([tampered copy](proofs/real-balanced_saver.tampered.proof.json)) |
| A3 | C4 growth investor (invented) | BIL 20.0%, SHY 14.4%, USO 5.6%, VNQ 16.2%, VT 4.4%, VTI 26.1%, XLV 13.2% | [`0x31b0e223c012adc1105128eac25038ea6d4d1cae891d9825e8c9848d7ffe7518`](https://sepolia.etherscan.io/tx/0x31b0e223c012adc1105128eac25038ea6d4d1cae891d9825e8c9848d7ffe7518) | 11,949,300 | 2026-10-19 16:16:36 UTC | Unchanged ✓ ([proof file](proofs/real-growth_investor.proof.json)) | Does not match ✗ ([tampered copy](proofs/real-growth_investor.tampered.proof.json)) |
| A4 | C5 crypto trader (invented) | BIL 15.0%, IEFA 9.2%, IGV 0.1%, SHY 3.0%, TRXUSDT 2.0%, VT 30.7%, VTI 40.0% | [`0xfe4e345ee9d7366d6dbecc4c5074e2374322fd4ca1c39ef2555d7201b43a0348`](https://sepolia.etherscan.io/tx/0xfe4e345ee9d7366d6dbecc4c5074e2374322fd4ca1c39ef2555d7201b43a0348) | 11,949,301 | 2026-10-19 16:16:48 UTC | Unchanged ✓ ([proof file](proofs/real-crypto_trader.proof.json)) | Does not match ✗ ([tampered copy](proofs/real-crypto_trader.tampered.proof.json)) |
| A5 | C3 balanced saver, confirmed by clicking in the browser (invented) | BIL 30.0%, SCHD 16.0%, SHY 30.0%, VIG 19.4%, VTI 4.6% | [`0xd36c957bee9577eb9726d0331f9927873efa4496b54c780acfe5e3b923ffa161`](https://sepolia.etherscan.io/tx/0xd36c957bee9577eb9726d0331f9927873efa4496b54c780acfe5e3b923ffa161) | 11,949,320 | 2026-10-19 16:20:48 UTC | Unchanged ✓ ([proof file](proofs/real-ui-balanced_saver.proof.json)) | — |
| C1 | Seal v2: the Verify page's **demo report** (real engine output, invented investor `demo_investor`) | the whole sealed report ([file](proofs/demo-sealed-report.v2.html)) | [`0x465e9db1f186b2c44cf1bc77755109baffc414ca81b5d946e6a87a69dd9dbd78`](https://sepolia.etherscan.io/tx/0x465e9db1f186b2c44cf1bc77755109baffc414ca81b5d946e6a87a69dd9dbd78) | 11,951,549 | 2026-10-19 23:53:24 UTC | Unchanged ✓ · Sealed for: demo_investor (live, 20 Oct) | Does not match ✗ ("Try a tampered copy": one weight changed) |
| C2 | Seal v2: **LIVE signed-in seal on the deployed site** (team demo account; build → news rated → Confirm and seal → Download sealed report → Verify a report) | the whole sealed report (kept by its account, not committed) | [`0x14f4f32ae446d706ae99f73ef75896b6d9443f23eff1cd25bd66cfa3f67dfaee`](https://sepolia.etherscan.io/tx/0x14f4f32ae446d706ae99f73ef75896b6d9443f23eff1cd25bd66cfa3f67dfaee) | 11,954,258 | 2026-10-20 08:59:24 UTC (12:59 Dubai); Etherscan: [screenshot](screenshots/17-etherscan-C2-live-seal.png), [input data](screenshots/18-etherscan-C2-input-data-EGOQ1.png) | Unchanged ✓ · sealed for the account's username | Does not match ✗ (one number changed in the downloaded file) |

*Seal v1 (A rows) fingerprints the advice record (risk category, answers, weights to the millionth, Risk Engine checks, versions).
Seal v2 (C rows, the final system) also fingerprints the exact report document, whose header says whom it was sealed for, so an edited
report or someone else's report cannot pass as the original. Old v1 proofs still verify.*

## Check it yourself in 2 minutes

1. **On the live site, no account:** open the EgoQuant website → sidebar **Verify a report** → **Use the demo report** →
   "Unchanged ✓ · Sealed for: demo_investor · … · block 11,951,549", then the original report. Click **Try a tampered copy** →
   "Does not match ✗: this report was changed after it was sealed". **View on Etherscan** opens the C1 transaction.
2. **With a file from this folder:** **Verify a report** → **Upload a sealed report** → choose
   [`proofs/demo-sealed-report.v2.html`](proofs/demo-sealed-report.v2.html) (Unchanged ✓) or a v1 proof file such as
   [`proofs/real-balanced_saver.proof.json`](proofs/real-balanced_saver.proof.json) (Unchanged ✓, "older seal format") and its
   [`tampered copy`](proofs/real-balanced_saver.tampered.proof.json) (Does not match ✗).
3. **Without our website (independent, Python 3 standard library only, no account, no key):** recompute the fingerprint from a proof
   file and compare it with the transaction on Etherscan. Run in this folder:
   ```bash
   python3 -c "import json,hashlib,sys;p=json.load(open(sys.argv[1],encoding='utf-8'));print(hashlib.sha256(bytes.fromhex(p['salt'])+p['record'].encode()).hexdigest())" proofs/real-balanced_saver.proof.json
   ```
   It prints `cc578d89…08ed335`. Open A2's transaction on Etherscan: its Input Data is `0x45474f5131` + exactly that value. The same
   command on `proofs/real-balanced_saver.tampered.proof.json` prints a different value, so the tampered copy does not match the chain.
   For the sealed report (seal v2), this also checks the document itself and shows whom it was sealed for:
   ```bash
   python3 -c "import json,hashlib,sys;t=open(sys.argv[1],encoding='utf-8',newline='').read();i=t.rfind('<!--EGOQ-SEAL-FOOTER-BEGIN-->');f=t[i:];o='<script type=\"application/json\" id=\"egoq-proof\">';a=f.find(o)+len(o);p=json.loads(f[a:f.find('</script>',a)]);r=json.loads(p['record']);print('document unchanged:',hashlib.sha256(t[:i].encode()).hexdigest()==r['versions']['report_document_sha256'],'| sealed for:',r['client']['username'],'| fingerprint:',hashlib.sha256(bytes.fromhex(p['salt'])+p['record'].encode()).hexdigest(),'| tx:',p['tx_hash'])" proofs/demo-sealed-report.v2.html
   ```
   It prints `document unchanged: True | sealed for: demo_investor | fingerprint: 02a7a1c7…f15aa6 | tx: 0x465e9db1…` and C1's Input
   Data on Etherscan is `0x45474f5131` + that fingerprint. (The project's own command `python -m egoquant.anchoring verify <file>` does the
   full check too, but it runs inside the server's configuration; the two lines above need nothing but Python.)

## What a seal proves, and what it does not

- **It proves** that this exact report existed, unchanged, **no later than** its block's time, sealed for that username.
- **It does not prove** that the advice was right, or who is holding the file. A printed or PDF copy cannot prove itself: only the sealed
  `.html` or a seal file can be checked. Sepolia is a test network and promises no permanence.
- **Privacy and deletion:** the record, its salt and the sealed report stay in the owner's account (row-level security, owner-only).
  "Delete my data" removes them, so the fingerprint can no longer be linked to the person; the chain entry itself cannot be deleted
  (the consent text says so). Someone who kept a downloaded sealed report still holds its own copy.
- **Safety:** test network only (any other chain id is refused); the signing key exists only in the server's environment; if the
  chain is down, the plan is still confirmed and the seal is sent later with the same fingerprint (advice is never blocked).
- **Roadmap (designed, not built):** the same fingerprint on a consortium chain such as WeBank's **FISCO BCOS**: the chain client and
  chain id change; the record, salt, commitment and verifier stay.

## Numbers and where they come from

| Claim | Source |
|---|---|
| 12 seals listed here, all re-checked on the chain on 20 Oct | [verify_snapshot.md](verify_snapshot.md) |
| Every committed proof still verifies with the final code and every tampered copy still fails (11 v1 proofs, 8 tampered copies, the v2 demo, byte-exact files) | `tests/anchoring/test_committed_proofs.py` (39 tests, passed on the deployed code `8f21177`) |
| One changed character anywhere, a changed username, a forged footer, old seals, owner-only "My seals" | `tests/anchoring/test_seal_v2.py` |
| 465 automated tests for the seal (464 run offline + 1 live test) | `tests/anchoring/` |
| The browser's verifier agrees with the backend's | frontend `scripts/check-vectors.mjs` (323 checks) |

## Screenshots ([screenshots/](screenshots/))

| File | Shows |
|---|---|
| `01-live-verify-demo-report-unchanged.jpg` | the **live** public site, 20 Oct: Use the demo report → Unchanged ✓ · Sealed for: demo_investor · block 11,951,549, all checks ✓ |
| `02-live-verify-tampered-copy-does-not-match.jpg` | the live site: Try a tampered copy → Does not match ✗; only "the report is exactly the sealed one" fails |
| `17-etherscan-C2-live-seal.png`, `18-etherscan-C2-input-data-EGOQ1.png` | Etherscan for **C2, the live signed-in seal** (tx `0x14f4f32a…7dfaee`, block 11,954,258, 20 Oct 08:59:24 UTC): Success, from = to = the sealing address, 0 ETH, Input Data `0x45474f5131` + the 32-byte commitment |
| `03-etherscan-A2-tx-full.jpg`, `04-etherscan-A2-input-data-EGOQ1.png` | Etherscan for A2: Success, from = to = the sealing address, 0 ETH, input `0x45474f5131…` |
| `05`–`16` (`-flow-…`) | the final user interface on a local run of the same code (19–20 Oct): waiting for the news rating, Confirm and seal, the consent, Sealed ✓ with Download sealed report, Verify (Unchanged ✓, the original shown), an edited report (Does not match ✗), the seal file (.json), the demo's tampered copy, Account → My seals, the sidebar's Verify a report (desktop and phone) |

## Part B: earlier CLI smoke tests (hand-written test records, NOT engine output)

Made on 19 Oct to test the sealing client before it was wired into the app. Real Sepolia transactions, but the numbers in the records
were written by hand.

| ID | Record | Transaction | Block | Block time | Verify | Tampered copy |
|---|---|---|---|---|---|---|
| B1 | hand-written C3 test record (3 holdings) | [`0xf3765c5f1ca3ac9836586fa6f69c770942ef73abeae4eaa351fb64c7551df239`](https://sepolia.etherscan.io/tx/0xf3765c5f1ca3ac9836586fa6f69c770942ef73abeae4eaa351fb64c7551df239) | 11,949,129 | 2026-10-19 15:42:00 UTC | Unchanged ✓ | Does not match ✗ |
| B2 | hand-written C1 test record (2 holdings) | [`0xa58785221e43e72c2548bd8a0b18d927ba26be4b2a598d2795fe7548f899cce9`](https://sepolia.etherscan.io/tx/0xa58785221e43e72c2548bd8a0b18d927ba26be4b2a598d2795fe7548f899cce9) | 11,949,207 | 2026-10-19 15:57:36 UTC | Unchanged ✓ | Does not match ✗ |
| B3 | hand-written C5 test record (5 holdings) | [`0xcdcf88edb48fd24cf7de43a373bc69a23d802347213b60612d47e3f03a1400f8`](https://sepolia.etherscan.io/tx/0xcdcf88edb48fd24cf7de43a373bc69a23d802347213b60612d47e3f03a1400f8) | 11,949,208 | 2026-10-19 15:57:48 UTC | Unchanged ✓ | Does not match ✗ |
| B4 | hand-written 25-fund test record | [`0xfd7e41b07c38ab0fe3d526578fa3a1dacaa7a0027f7399c2bbf99107df5670c9`](https://sepolia.etherscan.io/tx/0xfd7e41b07c38ab0fe3d526578fa3a1dacaa7a0027f7399c2bbf99107df5670c9) | 11,949,209 | 2026-10-19 15:58:00 UTC | Unchanged ✓ | Does not match ✗ |
| B5 | hand-written C3 test record, confirmed in the browser | [`0x7430fc73f72a1f4e753a434e62c8ea8cbc117aafcedc217f39015354e1546dbd`](https://sepolia.etherscan.io/tx/0x7430fc73f72a1f4e753a434e62c8ea8cbc117aafcedc217f39015354e1546dbd) | 11,949,221 | 2026-10-19 16:00:36 UTC | Unchanged ✓ | — |
