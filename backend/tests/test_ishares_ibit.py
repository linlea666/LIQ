from sources.ishares_ibit import ISharesIBITSource


def test_parse_ibit_official_holdings_csv():
    payload = ISharesIBITSource.parse_holdings('''iShares Bitcoin Trust ETF
Fund Holdings as of,"Aug 18, 2026"
Inception Date,"Jan 05, 2024"
Shares Outstanding,"1,322,560,000.00"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Market Currency,Accrual Date
"BTC","BITCOIN","-","Alternative","48,690,425,120.38","100.00","48,690,425,120.38","751,188.98830","BTC","-"
''')
    assert payload is not None
    assert payload["as_of"] == "2026-08-18"
    assert payload["shares_outstanding"] == 1_322_560_000
    assert payload["bitcoin_quantity"] == 751_188.9883
    assert payload["bitcoin_market_value_usd"] == 48_690_425_120.38


def test_parse_ibit_rejects_missing_btc_holding():
    assert ISharesIBITSource.parse_holdings("iShares Bitcoin Trust ETF\n") is None
