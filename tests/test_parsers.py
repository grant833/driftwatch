from datetime import UTC, datetime

from driftwatch.config import parse_blocklist
from driftwatch.guards import can_trade
from driftwatch.ingest.alpaca_news import normalize
from driftwatch.ingest.edgar import parse_feed, parse_ticker_map
from driftwatch.ingest.gdelt import parse_timeline

# ruff: noqa: E501

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>8-K - Example Corp (0000123456) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/123456/000012345626000010/0000123456-26-000010-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-10-06 &lt;b&gt;AccNo:&lt;/b&gt; 0000123456-26-000010 &lt;b&gt;Size:&lt;/b&gt; 300 KB&lt;br&gt;Item 2.02: Results of Operations&lt;br&gt;Item 9.01: Financial Statements</summary>
<updated>2026-10-06T16:05:12-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000123456-26-000010</id>
</entry>
<entry>
<title>4 - Doe Jane (0009999999) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/x-index.htm"/>
<summary type="html">Filed: 2026-10-06</summary>
<updated>2026-10-06T16:10:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0009999999-26-000001</id>
</entry>
<entry>
<title>4 - Example Corp (0000123456) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/x-index.htm"/>
<summary type="html">Filed: 2026-10-06</summary>
<updated>2026-10-06T16:10:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0009999999-26-000001</id>
</entry>
</feed>"""


def test_edgar_parse_items_and_issuer_preference():
    tmap = parse_ticker_map({"0": {"cik_str": 123456, "ticker": "exmp", "title": "Example"}})
    filings = {f["accession"]: f for f in parse_feed(ATOM, tmap)}
    eight_k = filings["0000123456-26-000010"]
    assert eight_k["form_type"] == "8-K"
    assert eight_k["items"] == ["2.02", "9.01"]
    assert eight_k["ticker"] == "EXMP"
    form4 = filings["0009999999-26-000001"]
    assert form4["role"] == "Issuer" and form4["ticker"] == "EXMP"


def test_alpaca_normalize():
    item = normalize({
        "T": "n", "id": 24918784, "headline": "Corsair buys majority of iDisplay",
        "summary": "", "author": "Benzinga Newsdesk", "created_at": "2022-01-05T22:00:37Z",
        "updated_at": "2022-01-05T22:00:38Z", "content": "<p>x</p>", "url": "https://b.com",
        "symbols": ["crsr", "CRSR"], "source": "benzinga",
    })
    assert item["external_id"] == "24918784"
    assert item["symbols"] == ["CRSR"]
    assert item["summary"] is None


def test_gdelt_timeline():
    pts = parse_timeline({"timeline": [{"series": "Average Tone", "data": [
        {"date": "20261006T120000Z", "value": -2.5}, {"date": "20261006T121500Z", "value": -1.0}]}]})
    assert pts[0] == (datetime(2026, 10, 6, 12, 0, tzinfo=UTC), -2.5)
    assert len(pts) == 2


def test_blocklist_and_guard():
    bl = parse_blocklist("# ubs model\nvoo\nSPY  # core\n\n")
    assert bl == frozenset({"VOO", "SPY"})
    assert can_trade("voo", bl)[0] is False
    assert can_trade("AAPL", bl)[0] is True
