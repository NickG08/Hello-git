"""
Builds the public page from the latest snapshot.

One template (site_template.html) feeds two outputs:

  site      a complete HTML document, served by kzbonds.serve
  artifact  the same page without the document skeleton, the form a Claude
            artifact is published in - used to preview design changes

The template holds the layout, styles and scripts. This module only fills in
the data: the bonds of the latest run, the note, the run id and its time.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd

from kzbonds import analysis, config, db

logger = logging.getLogger("kzbonds.site")

TEMPLATE_PATH = Path(__file__).with_name("site_template.html")

# The fields the page reads, in the order they are written to the page.
SITE_COLUMNS = [
    "exchange",
    "ticker",
    "isin",
    "issuer",
    "currency",
    "instrument_type",
    "face_value",
    "face_currency",
    "clean_price",
    "dirty_price",
    "accrued_interest",
    "coupon_rate",
    "coupon_freq",
    "day_count_basis",
    "maturity_date",
    "days_to_maturity",
    "ytm",
    "macaulay_duration",
    "modified_duration",
    "issue_volume",
    "price_quality",
    "price_quality_detail",
    "bond_kind",
    "is_perpetual",
    "last_trade_date",
    "price_age_days",
    "data_note",
    "listing_board",
    "listing_category",
    "listing_list",
    "listing_sector",
    "placement_type",
    "lot_size",
]

_DATE_COLUMNS = ("maturity_date", "last_trade_date")
_HEAD_END = "</style>\n"
_DOCUMENT = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{head}</head>
<body>
{body}</body>
</html>
"""


def snapshot_records(df: pd.DataFrame) -> list[dict]:
    """Rows of one run as plain values the page's script can read."""
    out = pd.DataFrame(index=df.index)
    for col in SITE_COLUMNS:
        out[col] = df[col] if col in df.columns else None

    for col in _DATE_COLUMNS:
        dates = pd.to_datetime(out[col], errors="coerce")
        out[col] = dates.dt.strftime("%Y-%m-%d").where(dates.notna(), None)

    out["is_perpetual"] = out["is_perpetual"].fillna(False).astype(bool)

    records = out.astype(object).where(out.notna(), None).to_dict(orient="records")
    for record in records:
        record["is_perpetual"] = bool(record["is_perpetual"])
    return records


def _snapshot_time(df: pd.DataFrame) -> pd.Timestamp | None:
    stamp = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True).max()
    return stamp if pd.notna(stamp) else None


def _generated_at(df: pd.DataFrame) -> str:
    """Shown as is when the page script cannot convert it to local time."""
    stamp = _snapshot_time(df)
    return stamp.strftime("%Y-%m-%d %H:%M GMT") if stamp is not None else "—"


def _generated_iso(df: pd.DataFrame) -> str:
    """Read by the page script, which shows it in the viewer's time zone."""
    stamp = _snapshot_time(df)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp is not None else ""


def _script_json(value) -> str:
    # "</" would end the <script> element early if it appeared in a string.
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def render(df: pd.DataFrame, mode: str = "site") -> str:
    """The page for one run. mode is "site" or "artifact"."""
    if mode not in ("site", "artifact"):
        raise ValueError(f"unknown mode: {mode}")
    if df.empty:
        raise ValueError("no snapshot to build the page from")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    page = (
        template.replace("__BONDS_JSON__", _script_json(snapshot_records(df)))
        .replace("__NOTE_JSON__", _script_json(analysis.generate_note(df)))
        .replace("__RUN_ID__", str(df["run_id"].iloc[0]))
        .replace("__GENERATED_AT__", _generated_at(df))
        .replace("__GENERATED_ISO__", _generated_iso(df))
    )
    if mode == "artifact":
        return page

    head, separator, body = page.partition(_HEAD_END)
    if not separator:
        raise ValueError("template has no </style> line to split head from body")
    return _DOCUMENT.format(head=head + separator, body=body)


def build(out_path: Path | None = None, mode: str = "site") -> Path | None:
    """Write the page for the latest run. Returns None when there is no run yet."""
    df = db.load_latest()
    if df.empty:
        logger.warning("No snapshot yet - page not built.")
        return None

    out_path = Path(out_path) if out_path else config.SITE_INDEX_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = render(df, mode=mode)

    # Write next to the target and swap, so a reader never gets half a page.
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_name, out_path)

    logger.info("Page built from run %s: %s (%d bytes)", df["run_id"].iloc[0], out_path, len(html))
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build the page from the latest snapshot.")
    parser.add_argument("--mode", choices=("site", "artifact"), default="site")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    result = build(args.out, mode=args.mode)
    print(result if result else "no snapshot")
