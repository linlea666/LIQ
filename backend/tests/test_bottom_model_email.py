import pytest

from config.settings import EmailNotificationConfig
from notifications.bottom_model_alert import build_bottom_model_email
from notifications.email_alert import send_html_email, send_html_email_result


def test_bottom_model_email_contains_evidence_quality_and_disclaimer():
    snapshot = {
        "day": "2026-08-12",
        "model_id": "bottom-v5",
        "data_policy_id": "pit-final-v2",
        "quadrant": {"key": "confirmed_recovery", "label": "高压力／多项确认"},
        "stress": {"score": 72.1},
        "confirmation": {
            "score": 81.2,
            "checks": [
                {"label": "aSOPR 收复 1", "ok": True, "status": "SCORABLE", "note": "14d 均 > 1"},
                {"label": "周线结构阶段", "ok": False, "status": "UNSCORABLE", "note": "数据不足"},
            ],
        },
        "evidence_quality": {"overall": 67.0},
        "factors": [{"label": "市场压力", "score": 72.1}],
        "data_quality": {"missing": [], "stale": [], "failed_fetches": {}},
    }
    subject, body = build_bottom_model_email(
        snapshot, page_url="http://example.com/bottom-model",
    )
    assert subject == "BTC 底部证据进入多项确认区"
    for text in [
        "2026-08-12", "市场压力", "aSOPR 收复 1", "周线结构阶段",
        "quality_status = OK", "不代表已经找到绝对最低点", "不是已校准概率",
        "http://example.com/bottom-model",
    ]:
        assert text in body


def test_bottom_model_email_escapes_snapshot_content():
    snapshot = {
        "day": "2026-08-12<script>",
        "quadrant": {"label": "<b>bad</b>"},
        "stress": {}, "confirmation": {"checks": []}, "factors": [],
        "data_quality": {},
    }
    _, body = build_bottom_model_email(snapshot)
    assert "<script>" not in body
    assert "<b>bad</b>" not in body


@pytest.mark.asyncio
async def test_detailed_email_result_keeps_boolean_interface_compatible():
    config = EmailNotificationConfig(enabled=True)
    detailed = await send_html_email_result("subject", "<p>body</p>", config)
    legacy = await send_html_email("subject", "<p>body</p>", config)
    assert detailed.ok is False and "incomplete" in detailed.error.lower()
    assert legacy is False
