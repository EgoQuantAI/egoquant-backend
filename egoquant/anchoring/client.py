"""Anchor clients: send a commitment to the chain, read seals back.

- `AnchorClient`: the interface the routes and the CLI use.
- `SepoliaAnchorClient`: plain JSON-RPC over `requests` (URL fallback) + `eth_account` for EIP-1559 signing.
  A 0-ETH transaction from the sealing address TO ITSELF, data = b"EGOQ1" + commitment.
- `FakeAnchorClient`: an in-memory chain for tests and offline demos (no network, no key).
- `anchor_client_from_env()`: the configured client, or None (then the feature is hidden).

The private key is read once from EGOQ_SEAL_PRIVATE_KEY into an `eth_account` object. It is never logged, printed,
returned, stored on this object as text or put into an exception message. Errors become short labels
(`error_kind`): rpc_down, rejected, nonce, cap, disabled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Protocol

import requests

from egoquant.anchoring import config as cfg
from egoquant.anchoring.commit import hex_to_bytes, parse_tx_data, tx_data

log = logging.getLogger(__name__)
TX_HASH = re.compile(r"0x[0-9a-f]{64}")             # strict: lower-case hex, exactly 32 bytes


@dataclass
class AnchorResult:
    status: str                      # pending | submitted | confirmed | failed
    tx_hash: str | None = None
    block_number: int | None = None
    block_time: str | None = None    # YYYY-MM-DDTHH:MM:SSZ
    error_kind: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TxInfo:
    """A transaction as the chain reports it (for /verify)."""
    tx_hash: str
    chain_id: int | None
    sender: str
    to: str | None
    data: bytes
    status: str                      # confirmed | pending | failed
    block_number: int | None = None
    block_time: str | None = None

    @property
    def commitment(self) -> bytes | None:
        return parse_tx_data(self.data)


class AnchorClient(Protocol):
    chain_id: int
    address: str
    explorer: str

    def anchor(self, commitment: bytes, *, on_signed: Callable[[str], None] | None = None) -> AnchorResult: ...
    def status(self, tx_hash: str) -> AnchorResult: ...
    def read_tx(self, tx_hash: str) -> TxInfo | None: ...
    def balance_wei(self) -> int: ...
    def budget(self, seconds: float) -> Any: ...       # context manager: total RPC time inside


def explorer_tx_url(explorer: str, tx_hash: str | None) -> str | None:
    return f"{explorer.rstrip('/')}/tx/{tx_hash}" if tx_hash else None


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hx(value: Any) -> int | None:
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else None


class RpcError(Exception):
    """`kind` is a label; never carries a node's message (it could quote our raw transaction)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class Rpc:
    """JSON-RPC over HTTPS with URL fallback. A transport error or an HTTP error moves to the next URL; a
    JSON-RPC error from a node that answered is final for that call."""

    def __init__(self, urls: tuple[str, ...], session: Any = None, timeout=cfg.RPC_TIMEOUT):
        if not urls:
            raise ValueError("no RPC URL")
        self.urls, self.timeout = tuple(urls), timeout
        self.session = session or requests.Session()
        self._local = threading.local()

    @contextmanager
    def budget(self, seconds: float):
        """Every call inside shares one deadline (all URLs and retries included); past it, calls fail fast
        with 'rpc_down'. Nested budgets keep the earlier deadline."""
        outer = getattr(self._local, "deadline", None)
        mine = time.monotonic() + seconds
        self._local.deadline = mine if outer is None else min(outer, mine)
        try:
            yield
        finally:
            self._local.deadline = outer

    def call(self, method: str, params: list) -> Any:
        deadline = getattr(self._local, "deadline", None)
        for url in self.urls:
            timeout = self.timeout
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0.05:
                    break
                timeout = (min(self.timeout[0], left), min(self.timeout[1], left))
            try:
                r = self.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=timeout)
                if r.status_code >= 400:
                    continue
                body = r.json()
            except (requests.RequestException, ValueError):
                continue
            if not isinstance(body, dict):
                continue
            if body.get("error") is not None:
                msg = str((body["error"] or {}).get("message", "")).lower() if isinstance(body["error"], dict) else ""
                if "already known" in msg or "known transaction" in msg:
                    raise RpcError("already_known")
                if "nonce" in msg:
                    raise RpcError("nonce")
                raise RpcError("rejected")
            return body.get("result")
        raise RpcError("rpc_down")


class SepoliaAnchorClient:
    """Seals on Sepolia. Only `address` is public; the key stays inside the eth_account object."""

    def __init__(self, private_key: str, *, rpc_urls: tuple[str, ...] = cfg.DEFAULT_RPC_URLS,
                 chain_id: int = cfg.SEPOLIA_CHAIN_ID, explorer: str = cfg.DEFAULT_EXPLORER,
                 gas_limit: int = cfg.GAS_LIMIT, session: Any = None):
        if chain_id not in cfg.ALLOWED_CHAIN_IDS:
            raise ValueError("only Sepolia (11155111) is allowed")
        from eth_account import Account
        try:
            self._account = Account.from_key(private_key)
        except Exception:
            raise ValueError("EGOQ_SEAL_PRIVATE_KEY is not a valid key") from None
        self.address: str = self._account.address
        self.chain_id, self.explorer, self.gas_limit = chain_id, explorer.rstrip("/"), gas_limit
        self.rpc = Rpc(rpc_urls, session)
        self._lock = Lock()                           # one nonce at a time in this process

    def budget(self, seconds: float):
        """`with client.budget(5):` caps the total time of every RPC call inside (routes use it)."""
        return self.rpc.budget(seconds)

    def __repr__(self) -> str:                        # never the key
        return f"SepoliaAnchorClient(address={self.address}, chain_id={self.chain_id})"

    # ---- sending ------------------------------------------------------------------------------
    def _fees(self) -> tuple[int, int]:
        try:
            tip = _hx(self.rpc.call("eth_maxPriorityFeePerGas", [])) or cfg.DEFAULT_TIP_WEI
        except RpcError as err:
            if err.kind == "rpc_down":
                raise
            tip = cfg.DEFAULT_TIP_WEI
        block = self.rpc.call("eth_getBlockByNumber", ["latest", False]) or {}
        base = _hx(block.get("baseFeePerGas")) or 0
        return cfg.FEE_BASE_MULTIPLIER * base + tip, tip

    def _sign(self, commitment: bytes, nonce: int, max_fee: int, tip: int) -> tuple[str, str]:
        tx = {"type": 2, "chainId": self.chain_id, "nonce": nonce, "to": self.address, "value": 0,
              "gas": self.gas_limit, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip,
              "data": "0x" + tx_data(commitment).hex()}
        try:
            signed = self._account.sign_transaction(tx)
        except Exception:
            raise RpcError("sign_failed") from None
        return "0x" + bytes(signed.raw_transaction).hex(), "0x" + bytes(signed.hash).hex()

    def anchor(self, commitment: bytes, *, on_signed: Callable[[str], None] | None = None) -> AnchorResult:
        """Sign and send one seal. The tx hash is known (and passed to `on_signed`) BEFORE the send. Never
        raises: failures come back as `pending` with an error label, to be retried with the same commitment."""
        with self._lock:
            try:
                nonce = _hx(self.rpc.call("eth_getTransactionCount", [self.address, "pending"]))
                if nonce is None:
                    return AnchorResult("pending", error_kind="rpc_down")
                max_fee, tip = self._fees()
                raw, tx_hash = self._sign(commitment, nonce, max_fee, tip)
            except RpcError as err:
                return AnchorResult("pending", error_kind=err.kind)
            if on_signed is not None:
                on_signed(tx_hash)
            try:
                self.rpc.call("eth_sendRawTransaction", [raw])
                return AnchorResult("submitted", tx_hash=tx_hash)
            except RpcError as err:
                if err.kind == "already_known":
                    return AnchorResult("submitted", tx_hash=tx_hash)
                # The send may or may not have gone through: ask for the hash before anything else.
                try:
                    if self.rpc.call("eth_getTransactionByHash", [tx_hash]):
                        return AnchorResult("submitted", tx_hash=tx_hash)
                except RpcError:
                    pass
                log.warning("seal send failed (%s)", err.kind)
                return AnchorResult("pending", error_kind=err.kind)

    # ---- reading ------------------------------------------------------------------------------
    def _block_time(self, number: int) -> str | None:
        block = self.rpc.call("eth_getBlockByNumber", [hex(number), False]) or {}
        return _iso(_hx(block.get("timestamp")))

    def status(self, tx_hash: str) -> AnchorResult:
        try:
            receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            if not receipt:
                known = self.rpc.call("eth_getTransactionByHash", [tx_hash])
                return AnchorResult("submitted" if known else "pending", tx_hash=tx_hash if known else None,
                                    error_kind=None if known else "not_found")
            number = _hx(receipt.get("blockNumber"))
            if _hx(receipt.get("status")) != 1:
                return AnchorResult("failed", tx_hash=tx_hash, block_number=number, error_kind="rejected")
            return AnchorResult("confirmed", tx_hash=tx_hash, block_number=number,
                                block_time=self._block_time(number) if number is not None else None)
        except RpcError as err:
            return AnchorResult("submitted", tx_hash=tx_hash, error_kind=err.kind)

    def read_tx(self, tx_hash: str) -> TxInfo | None:
        """The transaction by hash, or None if the chain does not know it. Raises RpcError('rpc_down')."""
        if not TX_HASH.fullmatch(tx_hash or ""):
            return None
        tx = self.rpc.call("eth_getTransactionByHash", [tx_hash])
        if not tx:
            return None
        receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash]) or {}
        number = _hx(receipt.get("blockNumber") or tx.get("blockNumber"))
        state = "pending" if not receipt else ("confirmed" if _hx(receipt.get("status")) == 1 else "failed")
        try:
            data = hex_to_bytes(tx.get("input") or tx.get("data") or "0x")
        except ValueError:
            data = b""
        return TxInfo(tx_hash=tx_hash, chain_id=_hx(tx.get("chainId")), sender=str(tx.get("from") or ""),
                      to=tx.get("to"), data=data, status=state, block_number=number,
                      block_time=self._block_time(number) if number is not None and receipt else None)

    def balance_wei(self) -> int:
        return _hx(self.rpc.call("eth_getBalance", [self.address, "latest"])) or 0


class FakeAnchorClient:
    """An in-memory chain: no network, no key. `down = True` makes every call fail like an unreachable RPC;
    `mine()` puts every submitted transaction into a block."""

    def __init__(self, address: str = "0x00000000000000000000000000000000000E9001",
                 chain_id: int = cfg.SEPOLIA_CHAIN_ID, explorer: str = cfg.DEFAULT_EXPLORER):
        self.address, self.chain_id, self.explorer = address, chain_id, explorer
        self.down = False
        self.sent: list[bytes] = []                   # every data payload ever sent (for "nothing else on-chain")
        self.txs: dict[str, dict] = {}
        self.block, self.nonce = 1_000_000, 0
        self._lock = Lock()

    def add_tx(self, tx_hash: str, *, sender: str, to: str | None, data: bytes, chain_id: int | None = None,
               mined: bool = True, ok: bool = True) -> None:
        """Put any transaction on the fake chain (tests: wrong sender, wrong prefix, ...)."""
        self.block += 1
        self.txs[tx_hash] = {"from": sender, "to": to, "data": data, "chain_id": chain_id or self.chain_id,
                             "block": self.block if mined else None, "time": 1_790_000_000 + self.block,
                             "ok": ok}

    def anchor(self, commitment: bytes, *, on_signed: Callable[[str], None] | None = None) -> AnchorResult:
        with self._lock:
            if self.down:
                return AnchorResult("pending", error_kind="rpc_down")
            data = tx_data(commitment)
            tx_hash = "0x" + hashlib.sha256(self.nonce.to_bytes(8, "big") + data).hexdigest()
            self.nonce += 1
            if on_signed is not None:
                on_signed(tx_hash)
            self.sent.append(data)
            self.txs[tx_hash] = {"from": self.address, "to": self.address, "data": data, "chain_id": self.chain_id,
                                 "block": None, "time": None, "ok": True}
            return AnchorResult("submitted", tx_hash=tx_hash)

    @contextmanager
    def budget(self, seconds: float):
        yield

    def mine(self) -> None:
        for tx in self.txs.values():
            if tx["block"] is None:
                self.block += 1
                tx["block"], tx["time"] = self.block, 1_790_000_000 + self.block

    def status(self, tx_hash: str) -> AnchorResult:
        if self.down:
            return AnchorResult("submitted", tx_hash=tx_hash, error_kind="rpc_down")
        tx = self.txs.get(tx_hash)
        if tx is None:
            return AnchorResult("pending", error_kind="not_found")
        if tx["block"] is None:
            return AnchorResult("submitted", tx_hash=tx_hash)
        if not tx["ok"]:
            return AnchorResult("failed", tx_hash=tx_hash, block_number=tx["block"], error_kind="rejected")
        return AnchorResult("confirmed", tx_hash=tx_hash, block_number=tx["block"], block_time=_iso(tx["time"]))

    def read_tx(self, tx_hash: str) -> TxInfo | None:
        if self.down:
            raise RpcError("rpc_down")
        tx = self.txs.get(tx_hash)
        if tx is None:
            return None
        state = "pending" if tx["block"] is None else ("confirmed" if tx["ok"] else "failed")
        return TxInfo(tx_hash=tx_hash, chain_id=tx["chain_id"], sender=tx["from"], to=tx["to"], data=tx["data"],
                      status=state, block_number=tx["block"],
                      block_time=_iso(tx["time"]) if tx["block"] is not None else None)

    def balance_wei(self) -> int:
        return 50_000_000_000_000_000


def anchor_client_from_env(*, require_enabled: bool = True, session: Any = None) -> AnchorClient | None:
    """The configured Sepolia client, or None when sealing is off or not configured (the feature is then
    hidden). Never raises and never logs the key; a bad key is logged as 'invalid', nothing more."""
    settings = cfg.settings_from_env()
    if require_enabled and not settings.enabled:
        return None
    if require_enabled and not os.getenv("EGOQ_SEAL_RPC_URLS", "").strip():
        log.warning("on-chain sealing is off: EGOQ_SEAL_RPC_URLS is not set")    # server: URLs only from env
        return None
    key = os.getenv("EGOQ_SEAL_PRIVATE_KEY", "").strip()
    if not key:
        return None
    try:
        return SepoliaAnchorClient(key, rpc_urls=settings.rpc_urls, chain_id=settings.chain_id,
                                   explorer=settings.explorer, session=session)
    except ValueError:
        log.warning("on-chain sealing is off: EGOQ_SEAL_PRIVATE_KEY is invalid or the chain id is not allowed")
        return None
    finally:
        del key


def published_addresses(client: AnchorClient | None) -> list[str]:
    """Every address /verify accepts as EgoQuant's: the current one first, then the earlier ones."""
    out = [client.address] if client is not None else []
    for a in cfg.settings_from_env().published_addresses:
        if a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


def wait_for_block(client: AnchorClient, tx_hash: str, *, timeout_s: float = 120.0, every_s: float = 4.0,
                   sleep: Callable[[float], None] = time.sleep) -> AnchorResult:
    """Poll the receipt until it is in a block or the time is up (CLI only; routes never wait like this)."""
    deadline = time.monotonic() + timeout_s
    result = client.status(tx_hash)
    while result.status == "submitted" and time.monotonic() < deadline:
        sleep(every_s)
        result = client.status(tx_hash)
    return result
