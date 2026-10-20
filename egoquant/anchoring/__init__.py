"""On-chain sealing of confirmed advisory records (docs/ANCHORING.md).

A confirmed, Risk-Engine-approved record is written as canonical JSON, salted and hashed (SHA-256); only the
32-byte commitment goes on-chain (Ethereum Sepolia testnet), as `EGOQ1` + commitment in a 0-ETH self-transaction.
The seal never changes a number, a limit or a verdict, and never blocks the advice.
"""
