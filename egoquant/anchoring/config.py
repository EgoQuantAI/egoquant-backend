"""Settings for on-chain sealing. Every number is PROVISIONAL and can be changed through the environment.

Environment (names only; values live in Railway / the local git-ignored .env):
    EGOQ_SEAL_ENABLED              "1" turns sealing on (anything else: the feature is hidden)
    EGOQ_SEAL_PRIVATE_KEY          the sealing wallet's key (secret; never logged, printed or returned)
    EGOQ_SEAL_RPC_URLS             comma-separated Sepolia JSON-RPC URLs, tried in order
    EGOQ_SEAL_CHAIN_ID             11155111 (Sepolia)
    EGOQ_SEAL_EXPLORER             https://sepolia.etherscan.io
    EGOQ_SEAL_PUBLISHED_ADDRESSES  earlier sealing addresses (comma-separated) that /verify still accepts
    EGOQ_SEAL_MAX_PER_USER_DAY     seals per user per day
    EGOQ_SEAL_MAX_PER_DAY          seals per day in total (this process)
    EGOQ_SEAL_BUDGET_S             seconds the confirm call may spend on the chain before answering
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

SEPOLIA_CHAIN_ID = 11155111
ALLOWED_CHAIN_IDS = (SEPOLIA_CHAIN_ID,)          # testnet only; mainnet is refused
DEFAULT_RPC_URLS = ("https://ethereum-sepolia-rpc.publicnode.com", "https://sepolia.drpc.org",
                    "https://1rpc.io/sepolia")   # public, no key; answered on 19 Oct 2026
DEFAULT_EXPLORER = "https://sepolia.etherscan.io"
TX_PREFIX = b"EGOQ1"                             # transaction data = prefix + 32-byte commitment
GAS_LIMIT = 30_000                               # PROVISIONAL; the intrinsic cost of a seal is 21 592 at most
RPC_TIMEOUT = (2.0, 4.0)                         # PROVISIONAL: connect, read (seconds) per RPC call
DEFAULT_TIP_WEI = 1_500_000_000                  # PROVISIONAL: 1.5 gwei if the node will not suggest a tip
FEE_BASE_MULTIPLIER = 2                          # max fee = 2 x base fee + tip (plan section 8)
RESEND_AFTER_S = 30 * 60                         # a submitted tx unknown to the node after this is re-sent
BUDGET_S = 6.0                                   # PROVISIONAL: confirm answers within this, whatever the chain does
MAX_PER_USER_DAY = 5                             # PROVISIONAL
MAX_PER_DAY = 200                                # PROVISIONAL
VERIFY_PER_MINUTE = 20                           # PROVISIONAL: /verify calls per IP per minute
VERIFY_GLOBAL_PER_MINUTE = 120                   # PROVISIONAL: all /verify calls together (behind Vercel the
                                                 # "IP" may be the proxy's, so this cap always applies)
VERIFY_RPC_BUDGET_S = 5.0                        # PROVISIONAL: total chain time for one /verify call
SEAL_CONSENT_FALLBACK = "2026-10-20-v2"             # until accounts.routes.SEAL_CONSENT_VERSION exists

CONSENT_TEXT = (
    "Confirm and seal this plan. EgoQuant will keep a record of this advice: your risk class, your questionnaire "
    "answers, the portfolio, the Risk Engine's checks and the steps you took, and the finished report exactly as you "
    "see it, marked \"Sealed for\" your username. We publish only a fingerprint of it (a 32-byte code) on a public test "
    "blockchain (Ethereum Sepolia), so anyone can later check that this exact report, sealed for your username, was not "
    "changed. The fingerprint itself contains no personal data. You can download the sealed report. A public blockchain "
    "entry can never be deleted. If you delete your data, we delete the record and its secret key, and we can then no longer "
    "link the fingerprint to you or to anything else (a sealed report you downloaded keeps its own copy of the record and "
    "key). This seals the advice; it places no order and moves no money."
)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.getenv(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class SealSettings:
    enabled: bool = False
    rpc_urls: tuple[str, ...] = DEFAULT_RPC_URLS
    chain_id: int = SEPOLIA_CHAIN_ID
    explorer: str = DEFAULT_EXPLORER
    published_addresses: tuple[str, ...] = field(default_factory=tuple)
    max_per_user_day: int = MAX_PER_USER_DAY
    max_per_day: int = MAX_PER_DAY
    budget_s: float = BUDGET_S


def settings_from_env() -> SealSettings:
    return SealSettings(
        enabled=os.getenv("EGOQ_SEAL_ENABLED", "").strip() == "1",
        rpc_urls=_list("EGOQ_SEAL_RPC_URLS") or DEFAULT_RPC_URLS,
        chain_id=_int("EGOQ_SEAL_CHAIN_ID", SEPOLIA_CHAIN_ID),
        explorer=(os.getenv("EGOQ_SEAL_EXPLORER", "").strip() or DEFAULT_EXPLORER).rstrip("/"),
        published_addresses=_list("EGOQ_SEAL_PUBLISHED_ADDRESSES"),
        max_per_user_day=_int("EGOQ_SEAL_MAX_PER_USER_DAY", MAX_PER_USER_DAY),
        max_per_day=_int("EGOQ_SEAL_MAX_PER_DAY", MAX_PER_DAY),
        budget_s=_float("EGOQ_SEAL_BUDGET_S", BUDGET_S),
    )


def seal_consent_version() -> str:
    """Onur's SEAL_CONSENT_VERSION once it exists (Bartu's decision, coordination file), else the fallback."""
    try:
        from egoquant.accounts import routes as accounts
        return getattr(accounts, "SEAL_CONSENT_VERSION", SEAL_CONSENT_FALLBACK)
    except Exception:                             # accounts not importable: the fallback is the same text version
        return SEAL_CONSENT_FALLBACK
