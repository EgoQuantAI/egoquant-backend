"""The "Seal" section of a downloaded report (HTML). Plain words, every value escaped, links only to the explorer.

Shown only when sealing is switched on (anchor_client_from_env); otherwise the report has no Seal section at all.
"""

from __future__ import annotations

from html import escape

from egoquant.anchoring.client import AnchorClient, explorer_tx_url

INSTRUCTIONS = ("To check this report wasn't changed: open EgoQuant → Verify a proof file → upload your proof file "
                "(Download proof file, on the report in your account).")
NOT_SEALED = "This plan has not been confirmed and sealed yet."
_WORDS = {
    "confirmed": "Sealed on the Sepolia test network ✓",
    "submitted": "Confirmed; the fingerprint was sent to the Sepolia test network and is waiting for a block.",
    "pending": "Confirmed; the fingerprint will be published to the Sepolia test network shortly.",
    "failed": "Confirmed; the fingerprint could not be published yet. Your confirmation is kept.",
}


def _date(value: str | None) -> str:
    return (value or "").replace("T", " ").replace("Z", " UTC")


def seal_section_html(client: AnchorClient, record: dict | None, seal: dict | None) -> str:
    """The section for one saved report: its seal, or one sentence saying it was not sealed."""
    if record is None:
        return f"<section><h2>Seal</h2><p>{escape(NOT_SEALED)}</p></section>"
    seal = seal or {"status": "pending"}
    status = seal.get("status") or "pending"
    rows: list[tuple[str, str]] = []
    tx = seal.get("tx_hash")
    if tx:
        url = explorer_tx_url(client.explorer, tx)
        rows.append(("Transaction", f"<a href='{escape(url, quote=True)}'>{escape(tx)}</a>"))
    if seal.get("block_number") is not None:
        rows.append(("Block", escape(f"{int(seal['block_number']):,}")
                     + (f" · {escape(_date(seal.get('block_time')))}" if seal.get("block_time") else "")))
    rows.append(("Fingerprint (commitment)", f"<code>{escape(str(record.get('commitment_hex', '')))}</code>"))
    address = seal.get("sealing_address") or client.address
    rows.append(("Sealed by", f"<a href='{escape(client.explorer + '/address/' + address, quote=True)}'>"
                              f"{escape(address)}</a> (EgoQuant's sealing address)"))
    table = "".join(f"<tr><th>{escape(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return (f"<section><h2>Seal</h2><p class='lede'>{escape(_WORDS.get(status, _WORDS['pending']))}</p>"
            f"<table class='seal'>{table}</table>"
            "<p class='small'>Only this fingerprint is public, never your report: it is a salted SHA-256 of the record "
            "of this advice, so it reveals nothing about you.</p>"
            f"<p>{escape(INSTRUCTIONS)}</p></section>")


def with_seal_section(page: str, section: str) -> str:
    """The report page with the section placed before its footer (or at the end of the page)."""
    for marker in ("<p class='foot'>", "</main>"):
        i = page.rfind(marker)
        if i != -1:
            return page[:i] + section + page[i:]
    return page + section
