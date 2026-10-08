"""Historical data for backtests, loaded once on the PC (needs internet; ~15-30 minutes).

  1. SEC "Insider Transactions Data Sets": every Form 4 since 2006, one ZIP per quarter
     of tab-separated files. We keep only open-market purchases (code P, acquired).
  2. SEC company records: industry code per issuer, to skip funds and SPACs exactly like
     the live strategy does.
  3. Alpaca daily bars (split- and dividend-adjusted, since 2016) for each event window.

Everything is resumable: finished quarters and bar batches are recorded, so a re-run
picks up where it stopped.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from .prices import DATA_URL, ET, bar_day, valid_symbol

log = logging.getLogger(__name__)

SEC_PATHS = ["structureddata", "datastandardsinnovation"]
SEC_URL = "https://www.sec.gov/files/{path}/data/insider-transactions-data-sets/{q}_form345.zip"
FIRST_QUARTER = "2016q1"          # Alpaca's free daily history starts in 2016
WINDOW_BEFORE = 160               # calendar days of history before a filing (beta, volatility)
WINDOW_AFTER = 50                 # calendar days after (20 trading days + lookback + slack)
csv.field_size_limit(10_000_000)


# ---------------- pure helpers (unit tested) ----------------

def quarters(first: str, today: date) -> list[str]:
    """All quarters from `first` through the last fully finished quarter."""
    y, q = int(first[:4]), int(first[-1])
    last_y, last_q = (today.year, (today.month - 1) // 3)
    if last_q == 0:
        last_y, last_q = last_y - 1, 4
    out = []
    while (y, q) <= (last_y, last_q):
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def quarter_bounds(qtr: str) -> tuple[date, date]:
    y, q = int(qtr[:4]), int(qtr[-1])
    start = date(y, 3 * q - 2, 1)
    end = date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)
    return start, end - timedelta(days=1)


def sec_date(s: str | None) -> date | None:
    """SEC data sets use DD-MON-YYYY (e.g. 05-JAN-2024)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.title() if fmt.startswith("%d") else s, fmt).date()
        except ValueError:
            continue
    return None


def num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def clean_ticker(s: str | None) -> str | None:
    """Normalise ISSUERTRADINGSYMBOL ('brk-b ', 'BRK/B', 'GOOG/GOOGL', 'NONE') to one
    symbol or None. 'X/Y' is a share class when Y is 1-2 letters, else two symbols
    (we take the first)."""
    t = (s or "").strip().upper()
    for sep in (",", ";", " "):
        t = t.split(sep)[0] if sep in t.strip() else t
    if "/" in t:
        head, tail = t.split("/", 1)
        t = f"{head}.{tail}" if 1 <= len(tail) <= 2 else head
    t = t.replace("-", ".")
    return t if valid_symbol(t) and t not in {"NONE", "NA", "N.A"} else None


def quarter_of(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def read_table(zf: zipfile.ZipFile, stem: str):
    """Yield dict rows from the member whose name matches `stem` (e.g. SUBMISSION)."""
    names = [n for n in zf.namelist()
             if n.rsplit("/", 1)[-1].split(".")[0].upper() == stem.upper()]
    if not names:
        raise KeyError(f"{stem} not found in archive ({zf.namelist()})")
    with zf.open(names[0]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def parse_quarter(zf: zipfile.ZipFile) -> tuple[list[tuple], list[tuple]]:
    """-> (trades, owners) restricted to Form 4 open-market purchases."""
    subs = {}
    for r in read_table(zf, "SUBMISSION"):
        doc = (r.get("DOCUMENT_TYPE") or "").strip()
        if doc not in ("4", "4/A"):
            continue
        plan = (r.get("AFF10B5ONE") or "").strip().upper() in ("1", "TRUE", "Y", "YES")
        subs[r["ACCESSION_NUMBER"]] = (sec_date(r.get("FILING_DATE")), doc,
                                       (r.get("ISSUERCIK") or "").strip() or None,
                                       clean_ticker(r.get("ISSUERTRADINGSYMBOL")), plan)
    trades, bad = [], 0
    for r in read_table(zf, "NONDERIV_TRANS"):
        try:
            acc = r["ACCESSION_NUMBER"]
            if acc not in subs or (r.get("TRANS_CODE") or "").strip().upper() != "P":
                continue
            if (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper() != "A":
                continue
            filed, doc, cik, tkr, plan = subs[acc]
            shares, price = num(r.get("TRANS_SHARES")), num(r.get("TRANS_PRICEPERSHARE"))
            if filed is None:
                continue
            value = shares * price if shares and price else None
            trades.append((acc, int(r["NONDERIV_TRANS_SK"]), filed, doc, cik, tkr,
                           sec_date(r.get("TRANS_DATE")), shares, price, value, plan))
        except (KeyError, ValueError, TypeError):
            bad += 1
    if bad:
        log.warning("skipped %d malformed transaction rows", bad)
    wanted = {t[0] for t in trades}
    owners = []
    for r in read_table(zf, "REPORTINGOWNER"):
        if r["ACCESSION_NUMBER"] not in wanted:
            continue
        rel = (r.get("RPTOWNER_RELATIONSHIP") or "").upper()
        owners.append((r["ACCESSION_NUMBER"], (r.get("RPTOWNERCIK") or "").strip(),
                       (r.get("RPTOWNERNAME") or "").strip(), "OFFICER" in rel,
                       "DIRECTOR" in rel, (r.get("RPTOWNER_TITLE") or "").strip() or None))
    return trades, owners


def merge_windows(days: list[date]) -> list[tuple[date, date]]:
    """Event dates -> merged [start, end] bar windows."""
    spans = sorted((d - timedelta(days=WINDOW_BEFORE), d + timedelta(days=WINDOW_AFTER))
                   for d in days)
    out: list[list[date]] = []
    for s, e in spans:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def in_windows(d: date, windows: list[tuple[date, date]]) -> bool:
    return any(s <= d <= e for s, e in windows)


# ---------------- loaders ----------------

def load_sec(conn, user_agent: str, today: date | None = None, only: list[str] | None = None,
             pause: float = 0.5) -> int:
    today = today or datetime.now(ET).date()
    done = {r[0] for r in conn.execute("SELECT quarter FROM bt_quarters")}
    todo = [q for q in (only or quarters(FIRST_QUARTER, today)) if q not in done]
    client = httpx.Client(headers={"User-Agent": user_agent}, timeout=120,
                          follow_redirects=True)
    total, missing = 0, []
    for q in todo:
        content = None
        for path in SEC_PATHS:
            resp = _get(client, SEC_URL.format(path=path, q=q))
            if resp.status_code == 200 and resp.content[:2] == b"PK":
                content = resp.content
                break
            if resp.status_code == 403:      # SEC's answer to throttling: back off, retry
                time.sleep(30)
                resp = _get(client, SEC_URL.format(path=path, q=q))
                if resp.status_code == 200 and resp.content[:2] == b"PK":
                    content = resp.content
                    break
                if resp.status_code == 403:
                    raise RuntimeError(f"SEC refused {q} twice (403): check SEC_USER_AGENT "
                                       "has your name and email, then re-run")
            if resp.status_code != 404:
                resp.raise_for_status()
            time.sleep(pause)
        if content is None:
            # SEC moved recent quarters to a second folder; a 404 on both means the
            # quarter isn't published yet (or the URL moved again: check the log).
            log.warning("SEC data set %s not found in %s; skipping", q, SEC_PATHS)
            missing.append(q)
            continue
        trades, owners = parse_quarter(zipfile.ZipFile(io.BytesIO(content)))
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO bt_insider_trades (accession, trans_sk, filing_date, doc_type, "
                "issuer_cik, ticker, trans_date, shares, price, value_usd, plan_10b5_1) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", trades)
            cur.executemany(
                "INSERT INTO bt_insider_owners (accession, owner_cik, owner_name, is_officer, "
                "is_director, title) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                owners)
            cur.execute("INSERT INTO bt_quarters (quarter, trades) VALUES (%s, %s)",
                        (q, len(trades)))
        total += len(trades)
        log.info("SEC %s: %d open-market purchases (%.1f MB)", q, len(trades),
                 len(content) / 1e6)
        time.sleep(pause)
    if missing:
        log.warning("quarters not loaded: %s", ", ".join(missing))
    return total


def _get(client: httpx.Client, url: str, params: dict | None = None,
         tries: int = 6) -> httpx.Response:
    """GET with retries on rate limits (429, honouring Retry-After), 5xx and network errors."""
    for attempt in range(tries):
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as exc:
            if attempt == tries - 1:
                raise
            log.warning("network error (%s); retrying", exc)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == tries - 1:
                return resp
            wait = resp.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else 2 ** attempt)
            continue
        return resp
    return resp


def load_sic(conn, sec_lookup, min_value: float = 25000, pause: float = 0.12) -> int:
    """Industry code for every issuer with a candidate purchase (cached in sec_companies)."""
    ciks = [r[0] for r in conn.execute(
        "SELECT DISTINCT lpad(t.issuer_cik, 10, '0') FROM bt_insider_trades t "
        "WHERE t.issuer_cik IS NOT NULL AND t.ticker IS NOT NULL AND t.value_usd >= %s "
        "AND lpad(t.issuer_cik, 10, '0') NOT IN (SELECT cik FROM sec_companies)",
        (min_value,))]
    n = 0
    for i, cik in enumerate(ciks):
        try:
            sic, desc = sec_lookup(cik)
        except Exception as exc:          # unknown issuer: record it so we don't retry
            log.debug("SIC lookup failed for %s: %s", cik, exc)
            sic, desc = None, None
        conn.execute("INSERT INTO sec_companies (cik, sic, sic_desc) VALUES (%s,%s,%s) "
                     "ON CONFLICT (cik) DO NOTHING", (cik, sic, desc))
        n += 1
        if i % 500 == 0:
            log.info("SIC codes: %d / %d", i, len(ciks))
        time.sleep(pause)                 # SEC asks for at most 10 requests per second
    return n


class BarClient:
    def __init__(self, key: str, secret: str):
        self.client = httpx.Client(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=60)

    def bars(self, symbols: list[str], start: date, end: date,
             asof: date | None = None) -> list[tuple]:
        """(ticker, day, open, low, close), adjusted. `asof` maps renamed symbols to the
        company that used the symbol on that date (so 2016's FB is today's META)."""
        params = {"symbols": ",".join(symbols), "timeframe": "1Day",
                  "start": start.isoformat(), "end": end.isoformat(), "adjustment": "all",
                  "feed": "sip", "limit": 10000}
        if asof:
            params["asof"] = asof.isoformat()
        rows: list[tuple] = []
        while True:
            resp = _get(self.client, f"{DATA_URL}/bars", params)
            if resp.status_code in (400, 422) and len(symbols) > 1:   # isolate a bad symbol
                mid = len(symbols) // 2
                return (self.bars(symbols[:mid], start, end, asof)
                        + self.bars(symbols[mid:], start, end, asof))
            if resp.status_code in (400, 422):
                log.warning("Alpaca rejected %s: %s", symbols[0], resp.text[:120])
                return []
            if resp.status_code == 403:
                raise PermissionError(f"Alpaca refused bar data (403): {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            for sym, bars in (data.get("bars") or {}).items():
                for b in bars or []:
                    rows.append((sym.upper(), bar_day(b["t"]), float(b["o"]), float(b["l"]),
                                 float(b["c"])))
            token = data.get("next_page_token")
            if not token:
                return rows
            params["page_token"] = token


def _insert_bars(conn, batch: str, rows) -> int:
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO bt_bars (batch, ticker, day, open, low, close) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        [(batch, *r) for r in rows])
    return len(rows)


def _replace_batch(conn, batch: str, rows) -> int:
    """All of a batch's bars come from one fetch, so they share one adjustment basis."""
    with conn.transaction():
        conn.execute("DELETE FROM bt_bars WHERE batch = %s", (batch,))
        conn.execute("DELETE FROM bt_bars_fetched WHERE batch = %s", (batch,))
        n = _insert_bars(conn, batch, rows)
        conn.execute("INSERT INTO bt_bars_fetched (batch, rows) VALUES (%s, %s)", (batch, n))
    return n


def load_bars(conn, client: BarClient, min_value: float = 25000,
              today: date | None = None) -> int:
    """SPY (refreshed every run) plus, per quarter, every candidate ticker's bars inside
    its event windows. A quarter whose windows reach past yesterday is refetched on the
    next run, so recent events fill in as time passes."""
    today = today or datetime.now(ET).date()
    yesterday = today - timedelta(days=1)
    n = _replace_batch(conn, "SPY", client.bars(["SPY"], date(2015, 6, 1), yesterday))
    log.info("SPY: %d bars", n)
    done = {r[0] for r in conn.execute("SELECT batch FROM bt_bars_fetched")}
    events = conn.execute(
        "SELECT DISTINCT ticker, filing_date FROM bt_insider_trades "
        "WHERE ticker IS NOT NULL AND value_usd >= %s AND filing_date >= '2016-01-01' "
        "ORDER BY 2", (min_value,)).fetchall()
    by_q: dict[str, dict[str, list[date]]] = defaultdict(lambda: defaultdict(list))
    for tkr, d in events:
        by_q[quarter_of(d)][tkr].append(d)
    total = 0
    for q in sorted(by_q):
        qs, qe = quarter_bounds(q)
        complete = qe + timedelta(days=WINDOW_AFTER) < yesterday
        if q in done and complete:
            continue
        tick = by_q[q]
        windows = {t: merge_windows(ds) for t, ds in tick.items()}
        start = max(qs - timedelta(days=WINDOW_BEFORE), date(2015, 6, 1))
        end = min(qe + timedelta(days=WINDOW_AFTER), yesterday)
        syms = sorted(tick)
        rows, got = [], set()
        # Map symbols as of the quarter's first day; symbols with no data then (e.g. a
        # ticker adopted after a mid-quarter rename, like META in June 2022) are retried
        # as of the quarter's last day.
        for asof, todo in ((qs, syms), (qe, None)):
            todo = todo if todo is not None else sorted(set(syms) - got)
            for i in range(0, len(todo), 100):
                for r in client.bars(todo[i:i + 100], start, end, asof=asof):
                    got.add(r[0])
                    if r[0] in windows and in_windows(r[1], windows[r[0]]):
                        rows.append(r)
        kept = _replace_batch(conn, q, rows)
        total += kept
        log.info("bars %s: %d tickers requested, %d with data, %d bars kept%s", q, len(syms),
                 len(got & set(syms)), kept, "" if complete else " (will refresh)")
    return total
