"""
AIX offering documents (profile `offeringMaterials`).

They live in AIX's K2 disclosure viewer (k4txdyb.onk2.com). There is no direct
file link: the public viewer page embeds an anonymous token and, on load, calls
its own AjaxCall endpoint, which returns the PDF as base64. This replays that
same call in one HTTP session - exactly what the page does for any visitor.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

from kzbonds import config

logger = logging.getLogger(__name__)

_BASE = "https://k4txdyb.onk2.com"
CACHE_FILE = config.DATA_DIR / "aix_docs_cache.json"


def document_id(viewer_url: str) -> str | None:
    ids = parse_qs(urlparse(viewer_url).query).get("documentid")
    return ids[0] if ids else None


def _init_call(controllers_js: str) -> tuple[str, str] | None:
    """(SmartObject guid, encrypted method) of the form's init call that loads the document."""
    m = re.search(
        r'ObjectID="([0-9a-f-]{36})" Method="([^"]+)"><Parameters><Parameter SourceType="FormParameter"'
        r' SourceID="documentid"[^>]*/></Parameters><Results>.*?TargetID="base64"',
        controllers_js,
        re.S,
    )
    return (m.group(1), m.group(2)) if m else None


def fetch_document_pdf(viewer_url: str, timeout: int = 60) -> bytes | None:
    if urlparse(viewer_url).path.lower().endswith(".pdf"):
        r = requests.get(viewer_url, timeout=timeout)
        return r.content if r.ok and r.content.startswith(b"%PDF") else None
    doc = document_id(viewer_url)
    if not doc or not viewer_url.startswith(_BASE):
        return None
    with requests.Session() as s:
        page = s.get(viewer_url, timeout=timeout).text
        token = re.search(r"__runtimeAnonToken\s*=\s*'([^']+)'", page)
        ctrl = re.search(r'src="(/Runtime/Runtime/AnonymousResources\.ashx\?method=GetControllers[^"]+)"', page)
        if not token or not ctrl:
            return None
        call = _init_call(html.unescape(s.get(_BASE + html.unescape(ctrl.group(1)), timeout=timeout).text))
        if not call:
            return None
        guid, method = call
        payload = [
            {
                "smartobject": {
                    "guid": guid,
                    "resultName": f"Primary_{guid}_{guid}",
                    "method": {"name": method},
                    "properties": {"ID": {"value": doc}},
                    "results": [
                        {
                            "targetID": "base64",
                            "targetType": "FormParameter",
                            "sourceType": "ObjectProperty",
                            "sourceID": "Document_EN_Memo",
                            "sourceInstanceID": None,
                            "targetInstanceID": None,
                        }
                    ],
                },
                "metadata": {
                    "methodexecuted": method,
                    "typeofview": "Capture",
                    "objectid": guid,
                    "contextIsSystem": False,
                },
            }
        ]
        r = s.post(
            _BASE + "/Runtime/Runtime/AjaxCall.ashx",
            json=payload,
            headers={"X-K2-Token": token.group(1), "Referer": viewer_url, "Origin": _BASE},
            timeout=timeout,
        )
    m = re.search(r'"Document_EN_Memo"\s*:\s*\{\s*"value"\s*:\s*"([A-Za-z0-9+/=\\]+)"', r.text)
    if r.status_code != 200 or not m:
        logger.warning("AIX document %s not returned (HTTP %s)", doc, r.status_code)
        return None
    pdf = base64.b64decode(m.group(1).replace("\\/", "/"))
    return pdf if pdf.startswith(b"%PDF") else None


_DATE = r"\d{2}\.\d{2}\.\d{4}"


def document_text(pdf: bytes, max_pages: int = 40) -> str:
    """Text layer if there is one, else OCR (AIX offer terms are often scans)."""
    from kzbonds.prospectus import _pdf_to_text

    text = _pdf_to_text(pdf)
    if len(text.strip()) >= 200 or shutil.which("tesseract") is None:
        return text
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "doc.pdf")
        with open(src, "wb") as f:
            f.write(pdf)
        subprocess.run(
            ["pdftoppm", "-r", "200", "-png", "-l", str(max_pages), src, os.path.join(d, "p")],
            capture_output=True,
            timeout=600,
            check=False,
        )
        pages = sorted(p for p in os.listdir(d) if p.endswith(".png"))
        return "\n".join(
            subprocess.run(
                ["tesseract", os.path.join(d, p), "stdout"], capture_output=True, timeout=120, check=False
            ).stdout.decode("utf-8", errors="ignore")
            for p in pages
        )


def _fix_ocr(text: str) -> str:
    # Tesseract reads the tenor digit of "5Y LPR" / "1Y SHIBOR" as a letter.
    text = re.sub(r"\bS(?=Y\b)", "5", text)
    return re.sub(r"\b[lI|](?=Y\b)", "1", text)


def parse_aix_terms(text: str) -> dict:
    """Coupon terms from AIX offer terms / prospectus text.

    The coupon schedule table ("Schedule of Payment of Interests") is the most
    reliable source: its rows give the accrual start, the period length and the
    rate formula, repeated on every row (so one OCR misread doesn't matter)."""
    text = _fix_ocr(text)
    flat = re.sub(r"\s+", " ", text)
    out: dict = {}
    rows = re.findall(rf"^\s*\d+\s+({_DATE})\s+(.+?)\s+{_DATE}\s+{_DATE}", text, re.M)
    if len(rows) >= 2:
        d0, d1 = (datetime.strptime(r[0], "%d.%m.%Y").date() for r in rows[:2])
        months = (d1.year - d0.year) * 12 + d1.month - d0.month
        if months in (1, 3, 6, 12):
            out["coupon_freq"] = 12 // months
        out["accrual_start"] = d0.isoformat()
        formulas = Counter(re.sub(r"\s*\+\s*", " + ", r[1]).strip() for r in rows)
        out["coupon_formula"] = formulas.most_common(1)[0][0]
    m = re.search(
        r"[Ff]loating interest rate, equal to the sum of the (.{2,30}?) and a margin ([\d.,]+) ?% per annum",
        flat,
    )
    if m:
        out.setdefault("coupon_formula", f"{m.group(1)} + {m.group(2)}%")
    if m or re.search(r"interest rate on (?:the )?bonds is floating", flat, re.I):
        out["coupon_status"] = "floating"
    m = re.search(r"PDA Rate of (\d+(?:[.,]\d+)?)\s*% per annum", flat)
    if m:
        out["coupon_rate"] = float(m.group(1).replace(",", "."))
        out["coupon_status"] = "sukuk_target"
    m = re.search(r"with (annual|semi-?annual|quarterly|monthly) capitali[sz]ation", flat, re.I)
    if m:
        out["capitalization_freq"] = {"annual": 1, "semiannual": 2, "quarterly": 4, "monthly": 12}[
            m.group(1).lower().replace("-", "")
        ]
    elif re.search(r"Coupon during the first year .{0,80}?accrued but not paid", flat, re.I):
        out["capitalization_freq"] = 1
    if "coupon_freq" not in out:
        for word, freq in (("monthly", 12), ("quarterly", 4), ("semi-annually", 2), ("annually", 1)):
            if re.search(rf"(?:payable|paid)\s+{word}", flat, re.I):
                out["coupon_freq"] = freq
                break
    return out


def coupon_note(terms: dict) -> str | None:
    if terms.get("coupon_status") == "sukuk_target" and terms.get("coupon_rate"):
        return f"Целевая ставка распределения {terms['coupon_rate']:g}% годовых (мудараба, не гарантирована)"
    if terms.get("capitalization_freq"):
        every = {1: "раз в год", 2: "раз в полгода", 4: "раз в квартал", 12: "раз в месяц"}[terms["capitalization_freq"]]
        return f"Купон капитализируется {every} и выплачивается при погашении"
    if terms.get("coupon_formula"):
        note = f"Плавающий купон: {terms['coupon_formula']}"
        if terms.get("accrual_start"):
            start = datetime.fromisoformat(terms["accrual_start"]).strftime("%d.%m.%Y")
            note += f", начисление с {start}"
        return note
    return None


def aix_document_terms(docs_by_ticker: dict[str, list[str]]) -> dict[str, dict]:
    """Merged terms per ticker from its offering documents, cached per document id."""
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    result = {}
    for ticker, urls in docs_by_ticker.items():
        merged: dict = {}
        for url in urls:
            doc = document_id(url) or url
            if doc not in cache:
                try:
                    pdf = fetch_document_pdf(url)
                    cache[doc] = parse_aix_terms(document_text(pdf)) if pdf else {}
                except Exception as exc:
                    logger.warning("AIX document %s could not be read: %s", doc, exc)
                    continue
            for k, v in cache[doc].items():
                merged.setdefault(k, v)
            if "capitalization_freq" in merged or (
                "coupon_freq" in merged and ("coupon_formula" in merged or "coupon_rate" in merged)
            ):
                break
        if merged:
            result[ticker] = merged
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return result
