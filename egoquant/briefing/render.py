"""HTML for the "Market Briefing (read-only)" section (Agent D) of the client report.

All text comes from outside (headlines, feed summaries, LLM output), so every string is HTML-escaped,
links must be http(s), and every rendered sentence passes the advice filter once more (last safety net).
"""

from __future__ import annotations

import html

from egoquant.briefing.advice_filter import SAFE_REPLACEMENT, AdviceFilter
from egoquant.briefing.loader import load_config
from egoquant.briefing.models import Briefing


def _e(x: object) -> str:
    return html.escape(str(x))


def _url(u: str) -> str:
    return _e(u) if u.lower().startswith(("https://", "http://")) else "#"


def tone_text(it) -> str:
    """'negative for holders of TLT; neutral for holders of BIL' (FinBERT fallback: 'negative wording (FinBERT)')."""
    parts = []
    for ft in it.fund_tones:
        parts.append(f"{ft.label} for holders of {ft.ticker}" if ft.method == "llm_fund_aware"
                     else f"{ft.label} wording, {ft.ticker} (FinBERT)")
    if parts:
        return "; ".join(parts)
    return f"{it.tone.tone:+.2f}" if it.tone else "not rated"


# Internal fallback reasons in the investor's words.
_PLAIN_REASONS = {"cache only": "the full news rating had not finished when this report was made",
                  "no fund-aware labeler given": "AI rating was not used for this report"}


def plain_reasons(b: Briefing) -> str:
    return "; ".join(dict.fromkeys(_PLAIN_REASONS.get(r, r) for r in b.metadata.tone_fallback_reasons))


def method_line(b: Briefing) -> str:
    m = b.metadata
    text = (f"Tone per headline and fund: {m.n_fund_tones_llm} AI-assessed for holders, "
            f"{m.n_fund_tones_finbert} FinBERT fallback")
    if m.n_fund_tones_unavailable:
        text += f", {m.n_fund_tones_unavailable} not rated (left out of the tone scores and alerts)"
    reasons = plain_reasons(b)
    return text + (f": {reasons}" if reasons and (m.n_fund_tones_unavailable or m.n_fund_tones_finbert) else "") + "."


def models_line(b: Briefing) -> str:
    """What actually scored and wrote the items, with counts: 'Tone: fund-aware gpt-4.1-mini (23), FinBERT
    fallback (0); summaries: gpt-4.1-mini (brief-v1) or a fixed template.'"""
    m = b.metadata
    tone = f"Tone: fund-aware {m.tone_llm_model or 'LLM'} ({m.n_fund_tones_llm}), FinBERT fallback ({m.n_fund_tones_finbert})"
    if m.n_fund_tones_unavailable:
        reasons = plain_reasons(b)
        tone += f", not rated ({m.n_fund_tones_unavailable}{': ' + reasons if reasons else ''})"
    if any(it.summary_kind == "llm" for it in b.items):
        summaries = f"summaries: {m.llm_model} ({m.prompt_version}) or a fixed template"
    else:
        summaries = "summaries: a fixed template (headline and source)"
    return f"{tone}; {summaries}."


def briefing_lines(b: Briefing) -> list[str]:
    """Every sentence of the section as plain text (used for rendering and for tests)."""
    lines = [*b.banners, *b.labels, models_line(b), b.tone_note, method_line(b)]
    lines += [a.message for a in b.alerts]
    lines += [n.text for n in b.suitability_notes]
    lines += [a.text for a in b.area_aggregates + b.instrument_aggregates]
    for it in b.items:
        lines += [it.summary, it.exposure_text, tone_text(it)]
    lines += [f"{d.name}: {d.reason}" for d in b.diversity_suggestions]
    return [x for x in lines if x]


def period_line(b: Briefing, prices_as_of=None) -> str:
    """'News 12 Oct to 19 Oct 2026 · prices as of 16 Oct 2026': the news period is not the price date."""
    text = f"News {b.period_start:%d %b} to {b.period_end:%d %b %Y}"
    return text + (f" · prices as of {prices_as_of:%d %b %Y}" if prices_as_of is not None else "")


def briefing_section_html(b: Briefing, advice: AdviceFilter | None = None, prices_as_of=None) -> str:
    advice = advice or AdviceFilter(load_config().forbidden_patterns)

    def say(text: str) -> str:
        return _e(advice.safe_line(text, SAFE_REPLACEMENT))

    m = b.metadata
    p = ["<section><h2>Market Briefing (read-only)</h2>"]
    for text in b.banners:
        p.append(f"<div class='banner'>{say(text)}</div>")
    p.append("<p class='small'>" + " · ".join(say(x) for x in b.labels) + ". "
             f"{say(period_line(b, prices_as_of))}. {say(models_line(b))}</p>")
    if b.tone_note:
        p.append(f"<p class='small'><b>{say(b.tone_note)}</b> {say(method_line(b))}</p>")
    if b.alerts:
        p.append("<ul class='reasons'>")
        p.extend(f"<li><b>Re-check offer.</b> {say(a.message)}</li>" for a in b.alerts)
        p.append("</ul>")
    if b.suitability_notes:
        p.append("<ul class='reasons'>" + "".join(f"<li>{say(n.text)}</li>" for n in b.suitability_notes) + "</ul>")
    if b.area_aggregates:
        p.append("<p class='small'>News tone by area and fund (-1 negative to +1 positive; not a forecast):</p><ul class='reasons'>")
        p.extend(f"<li>{say(a.text)}</li>" for a in b.instrument_aggregates)
        p.extend(f"<li>{say(a.text)}</li>" for a in b.area_aggregates)
        p.append("</ul>")
    p.append("<table><tr><th>Item</th><th>Tone</th><th>Source</th></tr>")
    for it in b.items:
        tone = tone_text(it)
        kind = {"llm": "AI-generated summary", "template": "headline", "withheld": "withheld"}[it.summary_kind]
        p.append(f"<tr><td>{say(it.summary)}<div class='small'>{say(it.exposure_text)} ({_e(kind)})</div></td>"
                 f"<td>{say(tone)}</td><td>{_e(it.item.source_name)}<div class='small'>"
                 f"{it.item.published_at:%d %b %Y} · <a href='{_url(it.item.url)}' rel='noopener noreferrer nofollow'>link</a>"
                 "</div></td></tr>")
    p.append("</table>")
    if b.diversity_suggestions:
        p.append("<p class='small'>To balance your sources, you could add:</p><ul class='reasons'>")
        p.extend(f"<li><a href='{_url(d.url)}' rel='noopener noreferrer nofollow'>{_e(d.name)}</a> "
                 f"({_e(d.source_type)}): {say(d.reason)}</li>" for d in b.diversity_suggestions)
        p.append("</ul>")
    p.append(f"<p class='small'>{m.n_fetched} items fetched, {m.n_matched} matched your instruments, "
             f"{m.n_unmatched} not relevant, {m.n_non_news} quote or listing pages, "
             f"{m.n_outside_window + (m.n_fetched - m.n_non_news - m.n_after_dedupe)} duplicates or "
             f"outside the window. {_e(m.cadence)}."
             + (" Skipped sources: " + _e("; ".join(m.sources_skipped)) + "." if m.sources_skipped else "")
             + "</p></section>")
    return "".join(p)
