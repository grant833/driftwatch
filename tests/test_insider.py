# ruff: noqa: E501
from datetime import date

from driftwatch.insider import is_signal, parse_form4

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <aff10b5One>0</aff10b5One>
  <issuer><issuerCik>0000123456</issuerCik><issuerName>Example Corp</issuerName>
    <issuerTradingSymbol>exmp</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0009999999</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-10-05</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10,000</value></transactionShares>
        <transactionPricePerShare><value>12.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>250000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-05</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode><footnoteId id="F1"/></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>12.60</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes><footnote id="F1">Sale effected pursuant to a Rule 10b5-1 trading plan.</footnote></footnotes>
</ownershipDocument>"""


def test_parse_form4():
    buy, sale = parse_form4(FORM4)
    assert buy["ticker"] == "EXMP" and buy["code"] == "P"
    assert buy["value_usd"] == 125000.0 and buy["shares_after"] == 250000.0
    assert buy["transaction_date"] == date(2026, 10, 5)
    assert buy["is_officer"] and not buy["is_director"]
    assert buy["officer_title"] == "Chief Executive Officer"
    assert buy["plan_10b5_1"] is False
    assert sale["code"] == "S" and sale["plan_10b5_1"] is True


def test_signal_filter():
    buy, sale = parse_form4(FORM4)
    assert is_signal(buy, 25000)
    assert not is_signal(sale, 25000)
    assert not is_signal({**buy, "value_usd": 10000.0}, 25000)
    assert not is_signal({**buy, "plan_10b5_1": True}, 25000)
    assert not is_signal({**buy, "is_officer": False, "is_director": False}, 25000)


def test_plan_checkbox_marks_all_lines():
    doc = FORM4.replace("<aff10b5One>0</aff10b5One>", "<aff10b5One>true</aff10b5One>")
    assert all(t["plan_10b5_1"] for t in parse_form4(doc))
