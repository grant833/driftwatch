"""Form 4 parser: turns insider filings into structured transactions.

The signal we care about is a discretionary open-market purchase (code P) by an
officer or director, not executed under a pre-arranged Rule 10b5-1 plan.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import httpx
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as ET

from .alerts import alert
from .config import Settings

log = logging.getLogger(__name__)


def _text(node, path: str) -> str | None:
    el = node.find(path) if node is not None else None
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "y", "yes"}


def _float(value: str | None) -> float | None:
    try:
        return float(value.replace(",", "")) if value else None
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def parse_form4(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    ticker = (_text(root, "issuer/issuerTradingSymbol") or "").upper() or None
    issuer_cik = _text(root, "issuer/issuerCik")

    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    name = _text(owner, "reportingOwnerId/rptOwnerName")
    is_director = _bool(_text(rel, "isDirector"))
    is_officer = _bool(_text(rel, "isOfficer"))
    is_ten = _bool(_text(rel, "isTenPercentOwner"))
    title = _text(rel, "officerTitle")

    # Plan flag: the 2023+ checkbox, or a footnote mentioning 10b5-1.
    doc_plan = _bool(_text(root, "aff10b5One"))
    footnotes = {fn.get("id"): (fn.text or "") for fn in root.iter("footnote")}

    trades = []
    for i, tx in enumerate(root.iter("nonDerivativeTransaction"), start=1):
        shares = _float(_text(tx, "transactionAmounts/transactionShares/value"))
        price = _float(_text(tx, "transactionAmounts/transactionPricePerShare/value"))
        refs = [el.get("id") for el in tx.iter("footnoteId")]
        tx_plan = any("10b5-1" in footnotes.get(r, "") for r in refs)
        trades.append({
            "line_no": i,
            "ticker": ticker,
            "issuer_cik": issuer_cik,
            "insider_name": name,
            "officer_title": title,
            "is_director": is_director,
            "is_officer": is_officer,
            "is_ten_pct_owner": is_ten,
            "transaction_date": _date(_text(tx, "transactionDate/value")),
            "code": _text(tx, "transactionCoding/transactionCode"),
            "acquired_disposed": _text(
                tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "shares": shares,
            "price": price,
            "value_usd": shares * price if shares is not None and price is not None else None,
            "shares_after": _float(_text(
                tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
            "plan_10b5_1": doc_plan or tx_plan,
        })
    return trades


def is_signal(t: dict, min_value: float) -> bool:
    return (
        t["code"] == "P"
        and t["acquired_disposed"] == "A"
        and not t["plan_10b5_1"]
        and (t["is_officer"] or t["is_director"])
        and (t["value_usd"] or 0) >= min_value
        and bool(t["ticker"])
    )


def find_xml_url(client: httpx.Client, index_url: str) -> str:
    base = index_url.rsplit("/", 1)[0]
    listing = client.get(f"{base}/index.json").raise_for_status().json()
    names = [item["name"] for item in listing["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml")]
    if not xmls:
        raise ValueError("no XML document in filing")
    return f"{base}/{xmls[0]}"


def insert_trades(conn, accession: str, trades: list[dict]) -> None:
    for t in trades:
        conn.execute(
            """
            INSERT INTO insider_trades (accession, line_no, ticker, issuer_cik, insider_name,
                officer_title, is_director, is_officer, is_ten_pct_owner, transaction_date, code,
                acquired_disposed, shares, price, value_usd, shares_after, plan_10b5_1)
            VALUES (%(accession)s, %(line_no)s, %(ticker)s, %(issuer_cik)s, %(insider_name)s,
                %(officer_title)s, %(is_director)s, %(is_officer)s, %(is_ten_pct_owner)s,
                %(transaction_date)s, %(code)s, %(acquired_disposed)s, %(shares)s, %(price)s,
                %(value_usd)s, %(shares_after)s, %(plan_10b5_1)s)
            ON CONFLICT DO NOTHING
            """,
            {**t, "accession": accession},
        )


def poll(s: Settings, conn) -> None:
    cfg = s.raw["insider"]
    client = httpx.Client(headers={"User-Agent": s.sec_user_agent}, timeout=30)
    failures = 0
    while True:
        rows = conn.execute(
            "SELECT accession, url FROM filings WHERE form_type IN ('4', '4/A') "
            "AND parsed_at IS NULL ORDER BY received_at LIMIT %s", (cfg["batch_size"],)
        ).fetchall()
        for accession, url in rows:
            try:
                xml = client.get(find_xml_url(client, url)).raise_for_status().text
                trades = parse_form4(xml)
                with conn.transaction():
                    insert_trades(conn, accession, trades)
                    conn.execute("UPDATE filings SET parsed_at = now() WHERE accession = %s",
                                 (accession,))
                for t in trades:
                    if is_signal(t, cfg["min_signal_value_usd"]):
                        log.info("INSIDER BUY %s: %s (%s) bought $%.0f",
                                 t["ticker"], t["insider_name"], t["officer_title"] or "director",
                                 t["value_usd"])
                failures = 0
            except (httpx.HTTPError, ValueError, ET.ParseError, KeyError,
                    DefusedXmlException) as exc:
                failures += 1
                log.warning("Form 4 %s failed: %s", accession, exc)
                conn.execute(
                    "UPDATE filings SET parsed_at = now(), parse_error = %s WHERE accession = %s",
                    (str(exc)[:500], accession))
                if failures == 10:
                    alert(s.slack_webhook, f"Form 4 parser failing repeatedly: {exc}")
            time.sleep(0.5)  # 2 filings/sec, well under SEC's limit (2 requests each)
        time.sleep(cfg["poll_seconds"] if not rows else 1)
