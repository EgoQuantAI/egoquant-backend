"""Match items to Agent C's catalog instruments and interest areas. Deterministic code, never the LLM.

Rules (in order of trust):
  0. A US exchange tag such as "(NASDAQ:SHY)" or "(NYSEARCA:SCHD)" is a mention of that ticker only (like a
     $cashtag). Every exchange tag is then removed, so the exchange name ("NASDAQ") never matches a keyword, and
     a NON-US listing ("(ASX:IVV)", "LSE:VUSA", "VXUS.DE") is never read as the US fund of the same ticker.
  1. $CASHTAG of a catalog ticker always matches.
  2. Keyword/alias match: the instrument's catalog name and aliases plus config keywords, as whole
     words. A keyword written in ALL CAPS is case-sensitive ("TIPS" must not match "tips"). A single STOCK's
     catalog name is case-sensitive too (a proper noun: "Target" the company, not "price target"), and names
     in config ambiguous_stock_names (e.g. "Dow": usually the index) are not used as keywords at all.
  3. A bare ticker matches only as an upper-case whole word (not before ".DE"-style listing suffixes), and never
     when it is in the config stopword list. Fund tickers that are English words (config capital_only_tickers:
     SHY, BIL, ...) match only in capitals as a whole token, not before a hyphen or apostrophe, and never in a
     headline written entirely in capitals ("Investors Shy Away" and "shy" never mean SHY).
Interest areas come from the matched instruments' catalog areas. R-levels are read from the catalog;
this module keeps no second list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from egoquant.agents.market_intel import DEFAULT_CATALOG, Instrument
from egoquant.briefing.loader import BriefingConfig
from egoquant.briefing.models import NewsItem

_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,9})\b")


_NOT_LISTING = r"(?!\.[A-Za-z]{1,3}\b)"          # "VXUS.DE", "AGG.MX": a foreign listing of the same symbol


def _mostly_capitals(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def _word_regex(term: str, case_sensitive: bool = False) -> re.Pattern[str]:
    flags = 0 if (case_sensitive or (term.isupper() and len(term) > 1)) else re.IGNORECASE
    return re.compile(r"(?<![\w$])" + re.escape(term) + r"(?!\w)", flags)


@dataclass(frozen=True)
class Match:
    tickers: tuple[str, ...]
    areas: tuple[str, ...]


# A fund named by its ticker: "... ETF (TLTE)", "... Trust (IBIT)", or an exchange tag "NYSEARCA:VYM".
_OTHER_FUND = re.compile(r"\b(?:ETF|Fund|Trust|Admiral|Index)\s*\(([A-Z]{2,5})\)|\b(?:NYSEARCA|NYSE|NASDAQ|BATS|CBOE)\s*:\s*([A-Z]{1,5})\b"
                         r"|\b([A-Z]{2,5})\s+(?:vs\.?|versus)\s+([A-Z]{2,5})\b")      # "VOO vs. IVV": a comparison of two funds


# One product named in the singular ("The S&P 500 ETF You Know", "... Index Fund Admiral Shares", "This Baron Emerging
# Markets Fund"): a write-up about THAT product. Plural market news ("equity funds draw inflows", "Bitcoin ETFs") is not.
_ONE_PRODUCT = re.compile(r"\b(?:ETF|fund)\b", re.IGNORECASE)


_FUND_KINDS = re.compile(r"\b(?:money[- ]market|hedge|pension|sovereign[- ]wealth|mutual|bond|equity|stock) funds?\b",
                         re.IGNORECASE)                                  # kinds of funds, not one product


def about_one_product(text: str) -> bool:
    return bool(_ONE_PRODUCT.search(_FUND_KINDS.sub(" ", text)))


def named_funds(text: str) -> set[str]:
    """Tickers of funds a headline names ("... ETF (DGS)", "... Admiral (VEMAX)", "NASDAQ:PFM", "VOO vs. IVV")."""
    return {t for pair in _OTHER_FUND.findall(text) for t in pair if t}


class Matcher:
    def __init__(self, config: BriefingConfig, catalog: list[Instrument] | None = None):
        self.catalog = list(catalog if catalog is not None else DEFAULT_CATALOG)
        self.by_ticker = {i.ticker: i for i in self.catalog}
        unknown = sorted(set(config.instrument_keywords) - set(self.by_ticker))
        if unknown:
            raise ValueError(f"instrument_keywords has tickers that are not in Agent C's catalog: {unknown}")
        self.stopwords = {t.upper() for t in config.ticker_stopwords}
        self.capital_only = {t.upper() for t in config.capital_only_tickers}
        self.patterns: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
        from egoquant.briefing.themes import RegionSignals
        self.region = RegionSignals(config)
        self.generic = {t.lower() for t in config.region_signals.generic_terms}
        ambiguous = {n.lower() for n in config.ambiguous_stock_names}
        for inst in self.catalog:
            is_stock = inst.product_type == "stock"
            names = [] if (is_stock and inst.name.lower() in ambiguous) else [inst.name]
            pats = [(n, _word_regex(n, case_sensitive=is_stock)) for n in names]
            terms = [*inst.aliases, *config.instrument_keywords.get(inst.ticker, [])]
            pats += [(t, _word_regex(t)) for t in dict.fromkeys(terms) if t.strip()]
            self.patterns[inst.ticker] = pats
        self.bare = {t: re.compile(r"(?<![\w$.])" + re.escape(t) + r"(?!\w)" + _NOT_LISTING) for t in self.by_ticker
                     if t.upper() not in self.stopwords and t.upper() not in self.capital_only}
        self.capital = {t: re.compile(r"(?<![\w$.'’-])" + re.escape(t) + r"(?![\w'’-])" + _NOT_LISTING)
                        for t in self.by_ticker if t.upper() in self.capital_only}
        prefixes = sorted(config.exchange_tag_prefixes, key=len, reverse=True)      # "NYSE ARCA" before "NYSE"
        alternatives = "|".join(r"\s*".join(re.escape(w) for w in p.split()) for p in prefixes)
        self.exchange_tag = (re.compile(r"\(\s*(?:" + alternatives + r")\s*:\s*([A-Za-z][A-Za-z0-9]{0,9})(?:\.[A-Za-z]{1,3})?\s*\)",
                                        re.IGNORECASE) if prefixes else None)
        # every other "(EXCHANGE:TICKER)" tag, and known non-US prefixes without parentheses: removed, never matched
        self.any_tag = re.compile(r"\(\s*[A-Za-z][A-Za-z ]{0,15}\s*:\s*[A-Za-z][A-Za-z0-9]{0,9}(?:\.[A-Za-z]{1,3})?\s*\)")
        foreign = sorted(config.non_us_exchange_prefixes, key=len, reverse=True)
        self.foreign_tag = (re.compile(r"\b(?:" + "|".join(map(re.escape, foreign)) + r")\s*:\s*[A-Za-z][A-Za-z0-9]{0,9}\b")
                            if foreign else None)

    def match_text(self, text: str) -> Match:
        found: set[str] = set()
        named = named_funds(text)                  # before the exchange tags are removed below
        listed_abroad = self.region.has_foreign(text)   # "(LSE:BBOX)" is a foreign signal; the tag is removed below
        one_product = about_one_product(text)
        crypto = self.region.has_crypto(text)
        if self.exchange_tag is not None:
            for tag in self.exchange_tag.findall(text):
                if tag.upper() in self.by_ticker:
                    found.add(tag.upper())
            text = self.exchange_tag.sub(" ", text)
        text = self.any_tag.sub(" ", text)
        if self.foreign_tag is not None:
            text = self.foreign_tag.sub(" ", text)
        for tag in _CASHTAG.findall(text):
            if tag.upper() in self.by_ticker:
                found.add(tag.upper())
        has_us = self.region.has_us(text)
        foreign_only = (self.region.has_foreign(text) or listed_abroad) and not has_us
        for ticker, pats in self.patterns.items():
            if ticker in found:
                continue
            hits = [term for term, p in pats if p.search(text)]
            if not hits:
                continue
            # A headline that names ANOTHER fund ("... Emerging Markets Bond ETF (EMBD)", "NYSEARCA:VYM") is about
            # that fund: topic words alone ("emerging markets") do not make it about ours; our name still does.
            inst = self.by_ticker[ticker]
            own = any(h.lower() == inst.name.lower() or h.upper() == ticker for h in hits)
            if named - {ticker} and not own:
                continue
            # crypto news is never stock-market news for an equity fund ("63 U.S. stocks tokenized on-chain")
            if crypto and not own and inst.asset_class == "equity":
                continue
            # a write-up about one other product: topic words alone do not count ("Bitcoin ETF" for IBIT still does)
            # (spot coins excepted: "Spot bitcoin ETF inflows" is demand for bitcoin itself)
            if (one_product and not own and inst.product_type != "crypto_spot"
                    and not any(_ONE_PRODUCT.search(h) or h.lower().endswith(("etfs", "funds")) for h in hits)):
                continue
            if self.region.is_us_fund(self.by_ticker[ticker]):
                # A keyword match for a US-region fund: never when the text is about a foreign government's bills,
                # currency or central bank; the generic bill/auction/yield terms also need a US signal.
                if foreign_only:
                    continue
                if all(h.lower() in self.generic for h in hits) and not has_us:
                    continue
            found.add(ticker)
        for ticker, pat in self.bare.items():
            if ticker not in found and pat.search(text):
                found.add(ticker)
        if not _mostly_capitals(text):
            for ticker, pat in self.capital.items():
                if ticker not in found and pat.search(text):
                    found.add(ticker)
        tickers = tuple(t for t in self.by_ticker if t in found)          # catalog order, deterministic
        areas = tuple(dict.fromkeys(a for t in tickers for a in self.by_ticker[t].areas))
        return Match(tickers=tickers, areas=areas)

    def match_item(self, item: NewsItem) -> Match:
        return self.match_text(f"{item.title}\n{item.summary_text}")
