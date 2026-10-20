"""SepoliaAnchorClient against a fake JSON-RPC node (real signing, no network), FakeAnchorClient, and
anchor_client_from_env. The key is made fresh per test run; no key is stored anywhere in the repository."""

import logging
import time

import requests

import pytest
pytest.importorskip("eth_account")              # skip without it; it is in requirements.txt
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

from egoquant.anchoring.client import (FakeAnchorClient, SepoliaAnchorClient, anchor_client_from_env,
                                       published_addresses)
from egoquant.anchoring.commit import tx_data
from tests.anchoring.fake_rpc import FakeRpcSession

URLS = ("https://rpc-a.invalid", "https://rpc-b.invalid")
COMMIT = bytes(range(32))


@pytest.fixture
def key():
    return Account.create().key.hex()


def make(key, session=None, **kw):
    session = session or FakeRpcSession()
    return SepoliaAnchorClient(key, rpc_urls=URLS, session=session, **kw), session


def test_tx_fields_self_send_zero_value_sepolia_and_exact_data(key):
    client, node = make(key)
    seen = []
    result = client.anchor(COMMIT, on_signed=seen.append)
    assert result.status == "submitted" and seen == [result.tx_hash]       # known before the send
    tx = TypedTransaction.from_bytes(HexBytes(node.raw_sent[0])).as_dict()
    sender = Account.recover_transaction(node.raw_sent[0])
    assert sender == client.address and "0x" + bytes(HexBytes(tx["to"])).hex() == client.address.lower()
    assert tx["value"] == 0 and tx["chainId"] == 11155111 and tx["gas"] == 30_000
    assert bytes(tx["data"]) == tx_data(COMMIT) and len(bytes(tx["data"])) == 37
    assert tx["maxFeePerGas"] == 2 * node.base_fee + 2_000_000_000


def test_receipt_confirms_with_block_and_time(key):
    client, node = make(key)
    r = client.anchor(COMMIT)
    assert client.status(r.tx_hash).status == "submitted"
    node.mine()
    st = client.status(r.tx_hash)
    assert st.status == "confirmed" and st.block_number == node.block and st.block_time.endswith("Z")
    info = client.read_tx(r.tx_hash)
    assert info.commitment == COMMIT and info.status == "confirmed" and info.chain_id == 11155111


def test_falls_back_to_the_second_url(key):
    client, node = make(key, FakeRpcSession(down={URLS[0]}))
    assert client.anchor(COMMIT).status == "submitted"
    assert any(url == URLS[1] for url, _ in node.calls)


def test_all_urls_down_gives_pending(key):
    client, node = make(key, FakeRpcSession(down=set(URLS)))
    r = client.anchor(COMMIT)
    assert r.status == "pending" and r.error_kind == "rpc_down" and r.tx_hash is None and not node.raw_sent


def test_send_that_timed_out_but_went_through_is_not_sent_twice(key):
    client, node = make(key, FakeRpcSession(send_timeout="after_accept"))
    r = client.anchor(COMMIT)
    assert r.status == "submitted" and len(node.raw_sent) == 1          # found by hash, not re-sent


def test_send_that_never_arrived_is_pending(key):
    client, node = make(key, FakeRpcSession(send_timeout="before_accept"))
    r = client.anchor(COMMIT)
    assert r.status == "pending" and not node.raw_sent


def test_nonces_increase(key):
    client, node = make(key)
    client.anchor(COMMIT)
    client.anchor(bytes(32))
    nonces = sorted(t["nonce"] for t in node.txs.values())
    assert nonces == [0, 1]


def test_mainnet_and_bad_keys_are_refused_without_echo(key):
    with pytest.raises(ValueError):
        SepoliaAnchorClient(key, chain_id=1, rpc_urls=URLS)
    bad = "0x" + "zz" * 32
    with pytest.raises(ValueError) as err:
        SepoliaAnchorClient(bad, rpc_urls=URLS)
    assert "zz" not in str(err.value)


def test_from_env_is_none_unless_enabled_and_keyed(monkeypatch, key, caplog):
    monkeypatch.delenv("EGOQ_SEAL_ENABLED", raising=False)
    monkeypatch.setenv("EGOQ_SEAL_PRIVATE_KEY", key)
    assert anchor_client_from_env() is None
    monkeypatch.setenv("EGOQ_SEAL_ENABLED", "1")
    monkeypatch.delenv("EGOQ_SEAL_PRIVATE_KEY")
    assert anchor_client_from_env() is None
    monkeypatch.setenv("EGOQ_SEAL_PRIVATE_KEY", "not-a-key")
    with caplog.at_level(logging.DEBUG):
        assert anchor_client_from_env() is None
    assert "not-a-key" not in caplog.text
    monkeypatch.setenv("EGOQ_SEAL_PRIVATE_KEY", key)
    monkeypatch.setenv("EGOQ_SEAL_CHAIN_ID", "1")                       # mainnet: refused
    assert anchor_client_from_env() is None
    monkeypatch.setenv("EGOQ_SEAL_CHAIN_ID", "11155111")
    monkeypatch.delenv("EGOQ_SEAL_RPC_URLS", raising=False)
    assert anchor_client_from_env(session=FakeRpcSession()) is None      # server: RPC URLs only from env
    monkeypatch.setenv("EGOQ_SEAL_RPC_URLS", ",".join(URLS))
    client = anchor_client_from_env(session=FakeRpcSession())
    assert client is not None and client.address == Account.from_key(key).address


def test_the_key_never_appears_in_repr_logs_or_results(key, caplog):
    raw = key.removeprefix("0x").lower()
    with caplog.at_level(logging.DEBUG):
        client, node = make(key, FakeRpcSession(down={URLS[0]}))
        r1 = client.anchor(COMMIT)
        node.mine()
        r2 = client.status(r1.tx_hash)
        down, _ = make(key, FakeRpcSession(down=set(URLS)))
        r3 = down.anchor(COMMIT)
    blob = " ".join([repr(client), str(vars(client)), caplog.text, str(r1), str(r2), str(r3),
                     str(client.read_tx(r1.tx_hash))]).lower()
    assert raw not in blob


def test_fake_client_down_and_mine():
    fake = FakeAnchorClient()
    fake.down = True
    assert fake.anchor(COMMIT).status == "pending" and fake.sent == []
    fake.down = False
    r = fake.anchor(COMMIT)
    assert fake.status(r.tx_hash).status == "submitted"
    fake.mine()
    assert fake.status(r.tx_hash).status == "confirmed"
    assert fake.sent == [tx_data(COMMIT)]


def test_published_addresses_current_first(monkeypatch):
    monkeypatch.setenv("EGOQ_SEAL_PUBLISHED_ADDRESSES", "0xOLD1, 0x00000000000000000000000000000000000E9001")
    fake = FakeAnchorClient()
    assert published_addresses(fake) == [fake.address, "0xOLD1"]


class _SlowSession(FakeRpcSession):
    def post(self, url, json=None, timeout=None):
        self.timeouts = getattr(self, "timeouts", []) + [timeout]
        time.sleep(0.2)
        raise requests.Timeout("slow")


def test_budget_caps_total_rpc_time_across_urls(key):
    node = _SlowSession()
    client, _ = make(key, node)
    client.rpc.urls = URLS * 10                                         # 20 slow URLs
    start = time.monotonic()
    with client.budget(0.5):
        assert client.anchor(COMMIT).status == "pending"
    assert time.monotonic() - start < 1.2
    assert all(t[0] <= 0.5 and t[1] <= 0.5 for t in node.timeouts)


def test_read_tx_refuses_malformed_hashes_without_calling(key):
    client, node = make(key)
    for bad in ("0x" + "AB" * 32, "0x" + "ab" * 31, "ab" * 32, "0x" + "ab" * 32 + "'", ""):
        assert client.read_tx(bad) is None
    assert node.calls == []


def test_seals_sent_at_the_same_moment_get_different_nonces(key):
    """Several confirms at once (one process, many request threads): the client's lock hands out one nonce at a time,
    so no seal replaces another in the mempool."""
    import threading
    client, node = make(key)
    start = threading.Barrier(8)
    results = []

    def seal(i):
        start.wait()
        results.append(client.anchor(bytes([i]) * 32))

    threads = [threading.Thread(target=seal, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 8 and all(r.status == "submitted" for r in results)
    assert sorted(t["nonce"] for t in node.txs.values()) == list(range(8))
    assert len({r.tx_hash for r in results}) == 8
