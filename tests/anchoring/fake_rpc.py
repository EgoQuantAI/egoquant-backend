"""A fake Ethereum JSON-RPC node behind a fake `requests` session: nonces, a mempool, blocks and receipts.

`SepoliaAnchorClient(..., session=FakeRpcSession(...))` runs its real signing and sending code against it, with
no network. `down` is a set of URLs that fail like an unreachable host; `drop_send` makes eth_sendRawTransaction
time out (once or always) after the node already accepted the tx, or before.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


@dataclass
class FakeRpcSession:
    chain_id: int = 11155111
    base_fee: int = 1_000_000_000
    down: set = field(default_factory=set)
    send_timeout: str | None = None              # None | "after_accept" | "before_accept"
    calls: list = field(default_factory=list)    # (url, method)
    raw_sent: list = field(default_factory=list)
    txs: dict = field(default_factory=dict)      # hash -> fields
    nonces: dict = field(default_factory=dict)
    block: int = 7_000_000

    def post(self, url, json=None, timeout=None):
        method, params = json["method"], json["params"]
        self.calls.append((url, method))
        if url in self.down:
            raise requests.ConnectionError("down")
        if method == "eth_sendRawTransaction" and self.send_timeout == "before_accept":
            raise requests.Timeout("timeout")
        try:
            result = getattr(self, "_" + method)(*params)
        except _RpcErr as err:
            return _Resp({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": err.msg}})
        if method == "eth_sendRawTransaction" and self.send_timeout == "after_accept":
            raise requests.Timeout("timeout")
        return _Resp({"jsonrpc": "2.0", "id": 1, "result": result})

    # ---- methods ------------------------------------------------------------------------------
    def _eth_getTransactionCount(self, addr, tag):
        return hex(self.nonces.get(addr.lower(), 0))

    def _eth_maxPriorityFeePerGas(self):
        return hex(2_000_000_000)

    def _eth_getBlockByNumber(self, tag, full):
        number = self.block if tag == "latest" else int(tag, 16)
        return {"number": hex(number), "baseFeePerGas": hex(self.base_fee), "timestamp": hex(1_790_000_000 + number)}

    def _eth_sendRawTransaction(self, raw):
        data = bytes.fromhex(raw[2:])
        tx = TypedTransaction.from_bytes(HexBytes(data))
        fields = tx.as_dict()
        sender = Account.recover_transaction(raw)
        h = "0x" + keccak(data).hex()                # the tx hash = keccak of the signed bytes
        if h in self.txs:
            raise _RpcErr("already known")
        if fields["nonce"] != self.nonces.get(sender.lower(), 0):
            raise _RpcErr("nonce too low")
        self.nonces[sender.lower()] = fields["nonce"] + 1
        self.raw_sent.append(raw)
        self.txs[h] = {"from": sender, "to": _addr(fields["to"]), "input": "0x" + bytes(fields["data"]).hex(),
                       "chainId": hex(fields["chainId"]), "value": fields["value"], "gas": fields["gas"],
                       "nonce": fields["nonce"], "block": None, "ok": True}
        return h

    def mine(self):
        for tx in self.txs.values():
            if tx["block"] is None:
                self.block += 1
                tx["block"] = self.block

    def _eth_getTransactionByHash(self, h):
        tx = self.txs.get(h)
        if tx is None:
            return None
        return {"hash": h, "from": tx["from"], "to": tx["to"], "input": tx["input"], "chainId": tx["chainId"],
                "blockNumber": hex(tx["block"]) if tx["block"] else None}

    def _eth_getTransactionReceipt(self, h):
        tx = self.txs.get(h)
        if tx is None or tx["block"] is None:
            return None
        return {"transactionHash": h, "blockNumber": hex(tx["block"]), "status": "0x1" if tx["ok"] else "0x0"}

    def _eth_getBalance(self, addr, tag):
        return hex(50_000_000_000_000_000)


def _addr(value):
    return to_checksum_address(value) if value else None


class _RpcErr(Exception):
    def __init__(self, msg):
        self.msg = msg
