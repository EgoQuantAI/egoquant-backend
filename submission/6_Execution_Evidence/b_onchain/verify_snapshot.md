# Live re-check of every listed seal transaction (2026-10-20 12:52 UTC)

Read-only, through a public Sepolia node (`https://ethereum-sepolia-rpc.publicnode.com`: `eth_getTransactionByHash`, `eth_getTransactionReceipt`,
`eth_getBlockByNumber`). No key, no account. Expected for every seal: status success, chain id 11155111, from = to = the sealing address
`0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC`, value 0, input = `0x45474f5131` (`EGOQ1`) + 32 bytes = 37 bytes.

| ID | Status | Chain | From = to = sealing address | Value | Input prefix | Input bytes | Block | Block time |
|---|---|---|---|---|---|---|---|---|
| A1 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,297 | 2026-10-19 16:16:00 UTC |
| A2 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,299 | 2026-10-19 16:16:24 UTC |
| A3 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,300 | 2026-10-19 16:16:36 UTC |
| A4 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,301 | 2026-10-19 16:16:48 UTC |
| A5 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,320 | 2026-10-19 16:20:48 UTC |
| B1 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,129 | 2026-10-19 15:42:00 UTC |
| B2 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,207 | 2026-10-19 15:57:36 UTC |
| B3 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,208 | 2026-10-19 15:57:48 UTC |
| B4 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,209 | 2026-10-19 15:58:00 UTC |
| B5 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,949,221 | 2026-10-19 16:00:36 UTC |
| C1 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,951,549 | 2026-10-19 23:53:24 UTC |
| C2 | success | 11155111 | yes | 0 | `0x45474f5131` | 37 | 11,954,258 | 2026-10-20 08:59:24 UTC |

**Result: 12 of 12 transactions match every expectation.**
