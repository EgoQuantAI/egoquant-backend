# On-chain sealing: evidence

Sealing address (public): `0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC`. Every seal is a 0-ETH transaction from that
address to itself, with input `0x45474f5131` (`EGOQ1`) + the 32-byte commitment `SHA-256(salt ‖ canonical record)`.
Nothing else goes on-chain. How it works and its limits: `docs/ANCHORING.md`.

**What is real and what is invented.** The **investors** are invented: the team's demo personas
(`egoquant/demo/personas.py`), with no real person, account or answers. In part A, everything else is real: the
portfolio, the Risk Engine's checks and the report were produced by EgoQuant's deployed engine (Railway, `POST
/portfolio`) on **real historical market data** (`data_source: historical`, prices as of 16 Oct 2026, real news
12–19 Oct). The report was then saved exactly as `/account/portfolio` saves it, and sealed through the real confirm
route. Part B holds the earlier CLI smoke tests: hand-written numbers that were **not** produced by the engine.

## A. Real engine output, real market data (invented investors)

| # | Investor (persona) | Allocation sealed | Transaction | Block (UTC) | Verify | Tampered copy |
|---|---|---|---|---|---|---|
| A1 | C1 no-loss saver (routes) | BIL 100.0% | [`0x26e84ac7c5…`](https://sepolia.etherscan.io/tx/0x26e84ac7c5c3317830ca1dfddd791ef96289d4fbdfa071bad1ba1f7cc40e33c5) | 11,949,297 (2026-10-19 16:16:00 UTC) | Unchanged ✓ | Does not match ✗ (hash differs) |
| A2 | C3 balanced saver (routes) | BIL 30.0%, SCHD 16.0%, SHY 30.0%, VIG 19.4%, VTI 4.6% | [`0x50617149a2…`](https://sepolia.etherscan.io/tx/0x50617149a2709deca1c068a9021f3e9b94aafc03f0a5e0d0cd1f41da23b7538c) | 11,949,299 (2026-10-19 16:16:24 UTC) | Unchanged ✓ | Does not match ✗ (hash differs) |
| A3 | C4 growth investor (routes) | BIL 20.0%, SHY 14.4%, USO 5.6%, VNQ 16.2%, VT 4.4%, VTI 26.1%, XLV 13.2% | [`0x31b0e223c0…`](https://sepolia.etherscan.io/tx/0x31b0e223c012adc1105128eac25038ea6d4d1cae891d9825e8c9848d7ffe7518) | 11,949,300 (2026-10-19 16:16:36 UTC) | Unchanged ✓ | Does not match ✗ (hash differs) |
| A4 | C5 crypto trader (routes) | BIL 15.0%, IEFA 9.2%, IGV 0.1%, SHY 3.0%, TRXUSDT 2.0%, VT 30.7%, VTI 40.0% | [`0xfe4e345ee9…`](https://sepolia.etherscan.io/tx/0xfe4e345ee9d7366d6dbecc4c5074e2374322fd4ca1c39ef2555d7201b43a0348) | 11,949,301 (2026-10-19 16:16:48 UTC) | Unchanged ✓ | Does not match ✗ (hash differs) |
| A5 | C3 balanced saver, confirmed by clicking in the browser (UI → routes) | BIL 30.0%, SCHD 16.0%, SHY 30.0%, VIG 19.4%, VTI 4.6% | [`0xd36c957bee…`](https://sepolia.etherscan.io/tx/0xd36c957bee9577eb9726d0331f9927873efa4496b54c780acfe5e3b923ffa161) | 11,949,320 (2026-10-19 16:20:48 UTC) | Unchanged ✓ | — |

How these were made:
- **A1–A4:** the deployed `/portfolio` built each persona's portfolio on real prices. A local copy of our routes
  saved the result as `/account/portfolio` does (with `inputs.answers` and both log rows), logged `report_viewed`,
  then `POST /account/reports/{id}/confirm` sealed it with the real Sepolia client.
- **A5:** the same real C3 report on the real **portfolio page** (local frontend with the wiring), confirmed by
  clicking "Confirm this plan" → consent → "Confirm and seal". The screenshots of this first run are not in the package; screenshots 05–16 in b_onchain show the final (seal v2) flow.
- Every proof file includes the saved report, so `/verify` also checks `versions.report_sha256` ("The full report
  matches the sealed fingerprint"). Each tampered copy moves one weight by 1 ppm. For the 100% BIL C1 portfolio,
  the weight moves down by 1 ppm: this case exposed a CLI bug that is now fixed and tested.
- **Broad portfolios:** on 19 Oct a C4 request with 20 interest areas made the deployed `/portfolio` answer 502 after
  249 s. **Fixed the same day** (commit `80be208`: the in-request news briefing is cache-only with a hard time budget;
  the full news rating runs as a background job). The deployed 20-interest-area build now takes **5.4 s** (live deploy
  check, 20 Oct: 71 of 71 passed). Part B's 25-fund test record covers the many-holdings case for sealing itself.

### Full values (part A)

**A1. C1 no-loss saver** (C1, 1 holding; data `historical` as of 2026-10-16)

- proof: `docs/anchoring/real-no_loss_saver.proof.json`
- commitment: `b3fa4daed194d4db91d0da8b64508659f8a1fc93956fe20599d66866a91d784c`
- tx: `0x26e84ac7c5c3317830ca1dfddd791ef96289d4fbdfa071bad1ba1f7cc40e33c5`
- https://sepolia.etherscan.io/tx/0x26e84ac7c5c3317830ca1dfddd791ef96289d4fbdfa071bad1ba1f7cc40e33c5
- block 11,949,297, 2026-10-19 16:16:00 UTC: Unchanged ✓; tampered: Does not match ✗ (hash differs)

**A2. C3 balanced saver** (C3, 5 holdings; data `historical` as of 2026-10-16)

- proof: `docs/anchoring/real-balanced_saver.proof.json`
- commitment: `cc578d89afe5a51b2604f202ac2680bf8827f40e56e5f1065b62539c908ed335`
- tx: `0x50617149a2709deca1c068a9021f3e9b94aafc03f0a5e0d0cd1f41da23b7538c`
- https://sepolia.etherscan.io/tx/0x50617149a2709deca1c068a9021f3e9b94aafc03f0a5e0d0cd1f41da23b7538c
- block 11,949,299, 2026-10-19 16:16:24 UTC: Unchanged ✓; tampered: Does not match ✗ (hash differs)

**A3. C4 growth investor** (C4, 7 holdings; data `historical` as of 2026-10-16)

- proof: `docs/anchoring/real-growth_investor.proof.json`
- commitment: `c9142a00329c65dab3d7cafa1329715944a78c50653e4da01af19cbbb1ff8d37`
- tx: `0x31b0e223c012adc1105128eac25038ea6d4d1cae891d9825e8c9848d7ffe7518`
- https://sepolia.etherscan.io/tx/0x31b0e223c012adc1105128eac25038ea6d4d1cae891d9825e8c9848d7ffe7518
- block 11,949,300, 2026-10-19 16:16:36 UTC: Unchanged ✓; tampered: Does not match ✗ (hash differs)

**A4. C5 crypto trader** (C5, 7 holdings; data `historical` as of 2026-10-16)

- proof: `docs/anchoring/real-crypto_trader.proof.json`
- commitment: `38c5938b5d206cd9fed5fbaf66232a3703b6990ee74652c080211346fe0fd543`
- tx: `0xfe4e345ee9d7366d6dbecc4c5074e2374322fd4ca1c39ef2555d7201b43a0348`
- https://sepolia.etherscan.io/tx/0xfe4e345ee9d7366d6dbecc4c5074e2374322fd4ca1c39ef2555d7201b43a0348
- block 11,949,301, 2026-10-19 16:16:48 UTC: Unchanged ✓; tampered: Does not match ✗ (hash differs)

**A5. C3 balanced saver, confirmed by clicking in the browser** (C3, 5 holdings; data `historical` as of 2026-10-16)

- proof: `docs/anchoring/real-ui-balanced_saver.proof.json`
- commitment: `5abe1889c58094db1feb6d8360531bc2e3e5b689cd1def64d26b3c55103362da`
- tx: `0xd36c957bee9577eb9726d0331f9927873efa4496b54c780acfe5e3b923ffa161`
- https://sepolia.etherscan.io/tx/0xd36c957bee9577eb9726d0331f9927873efa4496b54c780acfe5e3b923ffa161
- block 11,949,320, 2026-10-19 16:20:48 UTC: Unchanged ✓; tampered: —

## B. CLI smoke tests (hand-written test records, not engine output)

Sealed earlier the same evening to test the CLI fallback and the routes. The numbers in these records were written
by hand (`egoquant/anchoring/demo_saved_report.json` and variants), so they are **not** EgoQuant advice. They still
prove the sealing itself: real Sepolia transactions, verified, tamper detected.

| # | Record | Holdings | Transaction | Block | Verify | Tampered copy |
|---|---|---|---|---|---|---|
| B1 | hand-written C3 test record (CLI) | 3 | [`0xf3765c5f1c…`](https://sepolia.etherscan.io/tx/0xf3765c5f1ca3ac9836586fa6f69c770942ef73abeae4eaa351fb64c7551df239) | 11,949,129 | Unchanged ✓ | Does not match ✗ (hash differs) |
| B2 | hand-written C1 test record (CLI) | 2 | [`0xa58785221e…`](https://sepolia.etherscan.io/tx/0xa58785221e43e72c2548bd8a0b18d927ba26be4b2a598d2795fe7548f899cce9) | 11,949,207 | Unchanged ✓ | Does not match ✗ (hash differs) |
| B3 | hand-written C5 test record (CLI) | 5 | [`0xcdcf88edb4…`](https://sepolia.etherscan.io/tx/0xcdcf88edb48fd24cf7de43a373bc69a23d802347213b60612d47e3f03a1400f8) | 11,949,208 | Unchanged ✓ | Does not match ✗ (hash differs) |
| B4 | hand-written 25-fund test record (CLI) | 25 | [`0xfd7e41b07c…`](https://sepolia.etherscan.io/tx/0xfd7e41b07c38ab0fe3d526578fa3a1dacaa7a0027f7399c2bbf99107df5670c9) | 11,949,209 | Unchanged ✓ | Does not match ✗ (hash differs) |
| B5 | hand-written C3 test record, confirmed in the browser (UI → routes) | 3 | [`0x7430fc73f7…`](https://sepolia.etherscan.io/tx/0x7430fc73f72a1f4e753a434e62c8ea8cbc117aafcedc217f39015354e1546dbd) | 11,949,221 | Unchanged ✓ | — |

## C. Seal v2 (the sealed report): the demo seal and a live signed-in seal

Seal v2 fingerprints the exact final report as well as the record (`docs/ANCHORING.md`, "Seal v2"). The chain side is
unchanged: the same 0-ETH self-transaction with `EGOQ1` + 32 bytes.

| # | What | Transaction | Block (UTC) | Verify | Tampered copy |
|---|---|---|---|---|---|
| C1 | The Verify page's demo report ("Use the demo report"): real engine output for an invented investor, sealed through the real confirm route | [`0x465e9db1f1…`](https://sepolia.etherscan.io/tx/0x465e9db1f186b2c44cf1bc77755109baffc414ca81b5d946e6a87a69dd9dbd78) | 11,951,549 (2026-10-19 23:53:24 UTC) | Unchanged ✓ (public site, 20 Oct 12:50 Dubai) | Does not match ✗ ("Try a tampered copy": one weight changed) |
| C2 | **Live, signed-in path on the deployed site** (`docs/JUDGE_GUIDE.md`, path B), done by a team member with a demo account: build, wait for the news rating, Confirm and seal, Download sealed report, upload on Verify a report | [`0x14f4f32ae4…`](https://sepolia.etherscan.io/tx/0x14f4f32ae446d706ae99f73ef75896b6d9443f23eff1cd25bd66cfa3f67dfaee) | 11,954,258 (2026-10-20 08:59:24 UTC, 12:59 Dubai) | Unchanged ✓ (the downloaded sealed report, uploaded on the live Verify page by Aslan: sealed for the demo account's username, block 11,954,258) | Does not match ✗ (one number changed in the downloaded file) |

C2 on the chain (read with a public node): status success, from the sealing address to itself, 0 ETH, input 37 bytes
(`EGOQ1` + commitment), 14,368 gas. C1 is checked in the tests against a read-only snapshot of its transaction
(`tests/anchoring/test_committed_proofs.py`; the file is `docs/anchoring/demo-sealed-report.v2.html`). C2's downloaded sealed report is included as b_onchain/proofs/C2-live-sealed-report.html.

## Screenshots

In the submission package, `submission/EgoQuant_Submission_20Oct2026/6_Execution_Evidence/b_onchain/screenshots/` (index in that folder's `README.md`):
- **Live public site (20 Oct):** "Use the demo report" → Unchanged ✓ · Sealed for: demo_investor · block 11,951,549; "Try a tampered
  copy" → Does not match ✗.
- **Etherscan (A2):** Success, from = to = the sealing address, 0 ETH, input `0x45474f5131cc578d…08ed335` (`EGOQ1` + commitment).
- **The final seal flow (seal v2):** waiting for the news rating, Confirm and seal, the consent, Sealed ✓ with Download sealed report,
  Verify a report (Unchanged ✓ and the original shown), an edited report (Does not match ✗), the seal file (.json), Account → My seals,
  the sidebar's "Verify a report".

*Earlier version (19 Oct, seal v1):* the first run's screens with the old buttons "Use the demo proof" and "Download proof file" are not
part of the package; they showed the A2 and A5 seals listed above.

## Check it yourself

```bash
python -m egoquant.anchoring verify docs/anchoring/real-balanced_saver.proof.json            # Unchanged ✓
python -m egoquant.anchoring verify docs/anchoring/real-balanced_saver.tampered.proof.json   # Does not match ✗ (hash differs)
```

*These two commands run with the server's configuration (they refuse to start without the sealing variables). Without
anything of ours: recompute the fingerprint with Python's standard library and compare it with the transaction on Etherscan:*

```bash
python3 -c "import json,hashlib,sys;p=json.load(open(sys.argv[1],encoding='utf-8'));print(hashlib.sha256(bytes.fromhex(p['salt'])+p['record'].encode()).hexdigest())" docs/anchoring/real-balanced_saver.proof.json
# cc578d89…08ed335 = A2's input data after 0x45474f5131
```

Or on the site: sidebar **Verify a report** → **Use the demo report** loads C1 (`public/demo-sealed-report.html` in the
frontend), and **Try a tampered copy** shows "Does not match ✗". A v1 proof file such as A2 can still be uploaded there.
