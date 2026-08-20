from sources.cftc_cot import CFTCBitcoinCOTSource


def test_parse_cftc_bitcoin_futures_only_report():
    payload = CFTCBitcoinCOTSource.parse_report("""
    <pre>
    BITCOIN - CHICAGO MERCANTILE EXCHANGE Code-133741
    FUTURES ONLY POSITIONS AS OF 07/28/26 |
    (5 Bitcoins) OPEN INTEREST: 20,019
    COMMITMENTS
    15,572 11,668 3,687 38 3,600 19,297 18,955 722 1,064
    CHANGES FROM 07/21/26 (CHANGE IN OPEN INTEREST: -508)
    -830 -1,680 452 -59 517 -437 -711 -71 203
    PERCENT OF OPEN INTEREST FOR EACH CATEGORY OF TRADERS
    </pre>
    """)
    assert payload is not None
    assert payload["report_date"] == "2026-07-28"
    assert payload["open_interest_contracts"] == 20_019
    assert payload["noncommercial_net"] == 3_904
    assert payload["noncommercial_net_change"] == 850
    assert payload["positions"]["commercial_short"] == 3_600


def test_parse_cftc_rejects_wrong_or_incomplete_market():
    assert CFTCBitcoinCOTSource.parse_report("MICRO BITCOIN Code-133742") is None
