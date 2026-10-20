"""Seal v2: the sealed document and the downloadable sealed report.

The SEALED DOCUMENT is the final client report rendered ONCE, at confirm, with "Sealed for: <username> · report <id>" in
its own header. It is stored byte-exact (advisory_records.sealed_document, owner-only) and never rendered again; the
record carries SHA-256 of its exact UTF-8 bytes (versions.report_document_sha256), and the record's commitment is on the
chain. So:
  (A) an edited report with a genuine proof: SHA-256(document) no longer equals the sealed value  -> "changed";
  (B) someone else's genuine report: it verifies, but it says, inside the fingerprint, whom it was sealed for, and
      the username cannot be changed without breaking the fingerprint.

The SEALED REPORT (the download) = the document + a delimited footer:
    <document bytes><!--EGOQ-SEAL-FOOTER-BEGIN--> visible Seal section + <script type="application/json" id="egoq-proof">
    {record, salt, commitment, chain_id, tx_hash, sealing_address} </script><!--EGOQ-SEAL-FOOTER-END-->
Verification splits at the LAST footer marker: everything before it must hash to report_document_sha256. Anything
added, removed or changed in that part fails; the footer only carries what the chain check needs.
"""

from __future__ import annotations

import hashlib
import json
from html import escape

FOOTER_BEGIN = "<!--EGOQ-SEAL-FOOTER-BEGIN-->"
FOOTER_END = "<!--EGOQ-SEAL-FOOTER-END-->"
PROOF_TAG_OPEN = '<script type="application/json" id="egoq-proof">'
PROOF_TAG_CLOSE = "</script>"
PROOF_V2 = "egoq-proof-2"
PDF_NOTE = ("A printed or PDF copy can't prove itself. Check the original sealed report (.html) or the seal file at "
            "EgoQuant → Verify a report.")


def document_sha256(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def sealed_document(report: dict, *, username: str, report_id: str) -> str:
    """The final client report as one HTML page, with whom it was sealed for in its own header. Rendered once."""
    from egoquant.briefing.final import render_report_html
    page = render_report_html(report)
    line = (f"<p class='sealed-for' style='margin:0 0 12px;padding:8px 12px;border:2px solid currentColor;"
            f"font-weight:600'>Sealed for: {escape(username)} · report {escape(report_id[:8])}</p>")
    start = page.find("<main")
    end = page.find(">", start) if start != -1 else -1
    if end == -1:                                   # no <main>: right after <body>, else at the very top
        start = page.find("<body")
        end = page.find(">", start) if start != -1 else -1
    return page[:end + 1] + line + page[end + 1:] if end != -1 else line + page


def _embedded_json(fields: dict) -> str:
    """JSON safe inside <script>: '</' is written '<\\/' (still valid JSON), so the tag cannot be closed early."""
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def sealed_report_html(document: str, proof_fields: dict, seal_section: str) -> str:
    """The downloadable sealed report: the exact document, then the footer (not part of the fingerprint)."""
    fields = {k: proof_fields[k] for k in ("record", "salt", "commitment", "chain_id", "tx_hash", "sealing_address")}
    fields = {"proof": PROOF_V2, **fields}
    note = f"<p class='small'>{escape(PDF_NOTE)}</p>"
    return (document + FOOTER_BEGIN + seal_section + note + PROOF_TAG_OPEN + _embedded_json(fields) + PROOF_TAG_CLOSE
            + FOOTER_END + "\n")


def split_sealed_report(page: str) -> tuple[str, dict] | None:
    """(document, proof fields) of a sealed report, or None when the page has no seal footer (never sealed)."""
    i = page.rfind(FOOTER_BEGIN)
    if i == -1:
        return None
    footer = page[i:]
    a = footer.find(PROOF_TAG_OPEN)
    b = footer.find(PROOF_TAG_CLOSE, a + len(PROOF_TAG_OPEN)) if a != -1 else -1
    if a == -1 or b == -1:
        return None
    try:
        fields = json.loads(footer[a + len(PROOF_TAG_OPEN):b])
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None
    return page[:i], fields


def proof_v2(fields: dict, document: str) -> dict:
    """The technical seal file (.json): record + salt + commitment + tx + the sealed document itself."""
    keep = ("record", "salt", "commitment", "chain_id", "tx_hash", "sealing_address")
    return {"proof": PROOF_V2, **{k: fields[k] for k in keep}, "document": document}
