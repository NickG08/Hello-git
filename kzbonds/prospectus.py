"""
Issue terms read from KASE prospectuses (Проспекты tab of a bond page).

KASE's API leaves some terms blank for issues placed directly on the exchange
(coupon rate, coupon schedule), but the prospectus always states them. Used
only for bonds whose API data has gaps; results are cached per document URL.

Text comes from `pdftotext` (poppler-utils). Scanned prospectuses have no text
layer and yield nothing - those keep the API values.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta

import requests

from kzbonds import config

logger = logging.getLogger(__name__)

DOCS_URL = "https://kase.kz/api/instruments/docs/{ticker}?language=ru"
CACHE_FILE = config.DATA_DIR / "prospectus_cache.json"
DOCS_CACHE_DAYS = 7

_NUMW = r"(?:\s*\([^)]{1,60}\))?"
_HOMOGLYPHS = str.maketrans(
    {"c": "с", "C": "С", "o": "о", "O": "О", "a": "а", "A": "А", "e": "е", "E": "Е",
     "p": "р", "P": "Р", "x": "х", "X": "Х"}
)
_WORD_NUM = {
    "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20,
}
_FREQ_PATTERNS = [
    (r"через каждый 1\s*\(один\)\s*месяц|каждый 1\s*\(один\)\s*месяц|monthly", 12),
    (r"quarterly", 4),
    (r"semi-annual|semiannual", 2),
    (r"annually|once a year", 1),
    (r"ежемесячно|12\s*\(двенадцать\)\s*раз в год|каждый месяц", 12),
    (r"ежеквартально|4\s*\(четыре\)\s*раза в год|каждые 3\s*\(три\)\s*месяца|один раз в квартал", 4),
    (r"2\s*\(два\)\s*раза в год|два раза в год|раз в полгода|каждые 6\s*\(шесть\)\s*месяцев|полугодов", 2),
    (r"1\s*\(один\)\s*раз в год|один раз в год|ежегодно|каждые 12\s*\(двенадцать\)\s*месяцев", 1),
]


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    # Some prospectuses mix Latin look-alikes into Russian words ("cтавка").
    text = re.sub(
        r"(?<=[А-Яа-яЁё])[cCoOaAeEpPxX]+|[cCoOaAeEpPxX]+(?=[А-Яа-яЁё])",
        lambda m: m.group(0).translate(_HOMOGLYPHS),
        text,
    )
    # E-signature boilerplate repeated on every page break, often mid-field.
    for pat in (
        r"Бұл құжат.{0,400}?тең\.?",
        r"Электрондық құжат.{0,300}?аласыз\.",
        r"Данный документ согласно пункту.{0,400}?носителе\.?",
        r"Электронный документ сформирован.{0,300}?\.kz\.?",
    ):
        text = re.sub(pat, "", text)
    return re.sub(r"\s+", " ", text)


def parse_term_months(text: str) -> float | None:
    for m in re.finditer(r"[Сс]рок обращения облигаций", text):
        seg = text[m.end() : m.end() + 260]
        seg = re.split(r"\b[4-9]\)\s|дата и способ погашения", seg)[0]
        seg = re.sub(r"\d+\s*/\s*\d+", " ", seg)  # page numbers like "6 / 18"
        y = re.search(r"(?:Лет|Годы|Года|Год):\s*(\d+)", seg)
        mo = re.search(r"Месяц\w*:\s*(\d+)", seg)
        d = re.search(r"(?:Дней|Дни|День):\s*(\d+)", seg)
        total = (
            (int(y.group(1)) * 12 if y else 0)
            + (int(mo.group(1)) if mo else 0)
            + (int(d.group(1)) / 30 if d else 0)
        )
        if total:
            return total
    m = re.search(r"period of (\d+) years", text)
    if m:
        return int(m.group(1)) * 12
    m = re.search(
        r"[Сс]рок обращения облигаций[^.]{0,80}?Годы\s+Месяцы\s+Дни\s+(\d+)\s+(\d+)\s+(\d+)", text
    )
    if m:
        y, mo, d = map(int, m.groups())
        return y * 12 + mo + d / 30
    m = re.search(r"[Сс]рок обращения[^.:]{0,60}:?\s*(?:[^.]{0,40}?)Дни:\s*(\d+)", text)
    if m:
        return int(m.group(1)) / 30
    return None


def parse_coupon(text: str) -> tuple[float | None, str | None]:
    """(rate % p.a., status). Status is 'fixed', 'at_placement' (set by the
    first placement auction, published by KASE, not in the prospectus) or
    'floating'; rate is None unless status is 'fixed'."""
    m = re.search(
        r"ставка основного вознаграждения:\s*(.{0,260}?)\s*дополнительное вознаграждение",
        text,
        re.I,
    )
    if m:
        v = m.group(1)
        if re.search(r"будет определ|определяется по итогам", v, re.I):
            return None, "at_placement"
        if re.search(r"плавающ|индексир", v, re.I):
            return None, "floating"
        n = re.search(r"(\d+(?:[.,]\d+)?)\s*%", v) or re.search(r"^\s*(\d+(?:[.,]\d+)?)\b", v)
        if n:
            return float(n.group(1).replace(",", ".")), "fixed"
        word = v.strip().split()[0].lower().strip(",.;") if v.strip() else ""
        if word in _WORD_NUM:
            return float(_WORD_NUM[word]), "fixed"
    m = re.search(r"(?:fixed )?coupon rate of (\d+(?:\.\d+)?)\s*% per annum", text)
    if m:
        return float(m.group(1)), "fixed"
    if re.search(
        r"ставк[аи] купонного вознаграждения[^.]{0,120}будет определ[яе]\w* по итогам", text, re.I
    ):
        return None, "at_placement"
    for pat in (
        r"[Сс]тавка основного вознаграждения[^%]{0,220}?(\d+(?:[.,]\d+)?)" + _NUMW + r"\s*%",
        r"[Сс]тавка купонного вознаграждения[^%]{0,160}?составляет\s*(\d+(?:[.,]\d+)?)"
        + _NUMW
        + r"\s*%",
        r"[Сс]тавка (?:купонного |основного )?вознаграждения[^%]{0,200}?(\d+(?:[.,]\d+)?)"
        + _NUMW
        + r"\s*%\s*(?:\([^)]*\)\s*)?годовых",
    ):
        m = re.search(pat, text)
        if m:
            return float(m.group(1).replace(",", ".")), "fixed"
    if re.search(r"ставка[^.]{0,60}является плавающей", text, re.I):
        return None, "floating"
    return None, None


def parse_coupon_freq(text: str) -> int | None:
    seg = re.search(r"(?:[Пп]ериодичность|[Сс]роки) выплаты вознаграждения.{0,600}", text)
    scope = seg.group(0) if seg else text
    for pat, freq in _FREQ_PATTERNS:
        if re.search(pat, scope, re.I):
            return freq
    return None


def parse_terms(text: str) -> dict:
    text = normalize_text(text)
    rate, status = parse_coupon(text)
    return {
        "term_months": parse_term_months(text),
        "coupon_rate": rate,
        "coupon_status": status,
        "coupon_freq": parse_coupon_freq(text),
    }


def _pdf_to_text(content: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(content)
        f.flush()
        out = subprocess.run(
            ["pdftotext", "-layout", f.name, "-"], capture_output=True, timeout=60, check=False
        )
    return out.stdout.decode("utf-8", errors="ignore")


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"docs": {}, "terms": {}}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _doc_urls(ticker: str, cache: dict) -> list[str]:
    hit = cache["docs"].get(ticker)
    if hit and datetime.fromisoformat(hit["at"]) > datetime.now() - timedelta(days=DOCS_CACHE_DAYS):
        return hit["urls"]
    try:
        data = requests.get(DOCS_URL.format(ticker=ticker), timeout=30).json()
    except Exception:
        return []
    if not isinstance(data, list):
        data = []
    # Newest year first, so an amendment ("изменения") wins over the original.
    years = sorted((y for y in data if isinstance(y, dict)), key=lambda y: -(y.get("year") or 0))
    urls = [d["url"] for y in years for d in y.get("docs", []) if isinstance(d, dict) and d.get("url")]
    urls.sort(key=lambda u: "izm" not in u.lower())
    cache["docs"][ticker] = {"at": datetime.now().isoformat(), "urls": urls}
    return urls


def _terms_for_url(url: str, cache: dict) -> dict:
    if url in cache["terms"]:
        return cache["terms"][url]
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        terms = parse_terms(_pdf_to_text(r.content))
    except Exception as exc:
        logger.warning("Prospectus %s could not be read: %s", url, exc)
        return {}
    cache["terms"][url] = terms
    return terms


def kase_prospectus_terms(tickers: list[str]) -> dict[str, dict]:
    """Merged terms per ticker; for each field the newest document that states it wins."""
    if not tickers:
        return {}
    if shutil.which("pdftotext") is None:
        logger.warning("pdftotext not installed - prospectus terms skipped for %d bonds", len(tickers))
        return {}
    cache = _load_cache()
    result = {}
    for t in tickers:
        merged: dict = {}
        for url in _doc_urls(t, cache):
            for k, v in _terms_for_url(url, cache).items():
                if v is not None and merged.get(k) is None:
                    merged[k] = v
        if merged:
            result[t] = merged
    _save_cache(cache)
    return result
