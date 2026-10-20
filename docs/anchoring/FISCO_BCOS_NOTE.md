# Design note: the same seal on FISCO BCOS (roadmap, not built)

*Status: proposal (`docs/ONCHAIN_DECISIONS.md` OC-19), 20 Oct 2026. Nothing here is implemented. It explains what would
change if a bank moved EgoQuant's seal from the Sepolia test network to a consortium chain such as WeBank's FISCO BCOS.*

## What stays exactly the same

- **The record and the commitment:** the canonical record (`egoq-record-1` / `egoq-record-2`), the random 32-byte salt
  and `SHA-256(salt ‖ record)` (`egoquant/anchoring/canonical.py`, `egoquant/anchoring/commit.py`). A 32-byte value is
  just data to any chain, so the fingerprint does not depend on the chain's own hash or signature scheme.
- **The sealed document and the sealed report** (`egoquant/anchoring/sealed.py`): the document hash, the footer and the
  browser check do not touch the chain.
- **Privacy:** only 32 bytes go on the chain; the record, salt and document stay in the bank's own database, owner-only,
  and "delete my data" works the same way.
- **Advice is never blocked** by the chain: the plan is confirmed and the seal is sent later with the same commitment (the badge reads "Sealing… (the fingerprint will be sent shortly; your plan is confirmed)").

## What changes

| Part | Sepolia today | FISCO BCOS |
|---|---|---|
| Chain client | `SepoliaAnchorClient` (Ethereum JSON-RPC, EIP-1559 fees) in `egoquant/anchoring/client.py` | a second client class behind the same `anchor` / `status` / `read_tx` interface, using the FISCO BCOS SDK (group id, block limit instead of fees; no gas paid in a currency) |
| Where the 32 bytes go | transaction input `EGOQ1` + 32 bytes in a 0-ETH self-transaction | a tiny contract `seal(bytes32)` that emits an event; the transaction and event carry the same 32 bytes |
| Signing | secp256k1 key in Railway's environment | the bank's key in an HSM or key-management service; FISCO BCOS also offers the Chinese national algorithms (SM2 signatures, SM3 hashes) for the transaction itself |
| Who can verify | anyone, with a public node or Etherscan | consortium members and anyone the consortium gives read access (for example a regulator, or a public read-only gateway) |
| Permanence | none promised (a test network) | set by the consortium's governance, which is the point of moving |
| Allowed chains | chain id 11155111 only (`ALLOWED_CHAIN_IDS`) | an explicit entry for the consortium chain and group, decided by Aslan and Savas in writing |
| Proof file | `chain_id`, `tx_hash`, `sealing_address` | the same fields plus the group id, so a seal file names its chain unambiguously; a new proof version, the old verifier kept |

## Optional, when volume grows

**Batching:** seal a Merkle root of many commitments in one transaction, and give each seal file its path to the
root. Each investor's proof stays independent, and the chain cost becomes one transaction per batch. It is a new,
versioned proof format; single seals and every existing proof keep verifying.

## How it would be done safely

1. Add the client and the contract behind a flag, off by default, with a fake chain for the tests (as `FakeAnchorClient` today).
2. Run both chains side by side on the same commitment for a trial period; the Verify page accepts both.
3. Switch the default only on Aslan's decision, after every existing Sepolia proof still verifies
   (`tests/anchoring/test_committed_proofs.py`).
