"""P4 天级画像测试。

覆盖：
  1) compute_zone_band_profiles：出现率/episode 寿命/事件去重兑现率/SR 测试
  2) wall_history_profile：从归档（jsonl + gz）刷新缓存 → attach 到 zone
"""

from __future__ import annotations

import gzip
import json
import time

import pytest

from models.orderbook_pressure import WallZone
from processors.liquidity_wall_postmortem import (
    EventRecord,
    KlinePoint,
    ZoneRecord,
    compute_zone_band_profiles,
)
from processors import wall_history_profile as whp


def _rec(zid: str, ts: int, *, side="bid", price_mid=99_100.0) -> ZoneRecord:
    return ZoneRecord(
        coin="BTC", ts=ts, wall_zone_id=zid, side=side,
        price_low=price_mid - 100, price_high=price_mid + 100,
        price_mid=price_mid, current_usd=1_000_000,
        trust_score=0.6, break_through_risk=0.2, wall_removal_risk=0.1,
        wall_consumed_confidence=0.0, support_resistance_trust_score=0.5,
        sweep_attractiveness_score=0.3, active_attack_score=0.1,
        persistence_score=0.5, persistence_min=30,
    )


def _ev(zid: str, ts: int, etype: str) -> EventRecord:
    return EventRecord(
        coin="BTC", ts=ts, wall_zone_id=zid, event_type=etype,
        side="bid", price_mid=99_100.0, confidence=0.7,
    )


class TestZoneBandProfiles:

    def test_presence_and_episode_split(self):
        t0 = 1_700_000_000 - (1_700_000_000 % 3600)   # 对齐小时边界
        # episode 1：3 帧跨 2 个小时桶；间隔 2h 后 episode 2：1 帧
        recs = [
            _rec("aaa", t0),
            _rec("aaa", t0 + 1800),
            _rec("aaa", t0 + 3600),
            _rec("aaa", t0 + 3600 + 7200),
        ]
        profiles = compute_zone_band_profiles(recs, [], klines=[])
        assert len(profiles) == 1
        p = profiles[0]
        assert p.frames_seen == 4
        assert p.hours_present == 3          # t0 桶、t0+1h 桶、t0+3h 桶
        assert p.episode_count == 2
        # episode 寿命：3600s 和 0s → 平均 30min
        assert p.avg_lifetime_min == pytest.approx(30.0, abs=0.1)
        # 未传 window_hours：观测窗口 3h → 3/3 = 100%
        assert p.presence_ratio == pytest.approx(1.0)

    def test_fixed_window_hours(self):
        t0 = 1_700_000_000 - (1_700_000_000 % 3600)
        recs = [_rec("aaa", t0), _rec("aaa", t0 + 3600)]
        profiles = compute_zone_band_profiles(
            recs, [], klines=[], window_hours=168.0,
        )
        assert profiles[0].presence_ratio == pytest.approx(2 / 168, abs=0.001)

    def test_event_dedup_and_consumed_ratio(self):
        t0 = 1_700_000_000
        recs = [_rec("aaa", t0)]
        # 同一 10min 桶内引擎重复发 3 次 consumed → 只算 1 次
        events = [
            _ev("aaa", t0 + 10, "wall_consumed"),
            _ev("aaa", t0 + 20, "wall_consumed"),
            _ev("aaa", t0 + 30, "wall_consumed"),
            _ev("aaa", t0 + 700, "wall_removed"),      # 另一桶
        ]
        p = compute_zone_band_profiles(recs, events, klines=[])[0]
        assert p.consumed_events == 1
        assert p.removed_events == 1
        assert p.consumed_ratio == pytest.approx(0.5)

    def test_no_events_ratio_none(self):
        p = compute_zone_band_profiles(
            [_rec("aaa", 1_700_000_000)], [], klines=[],
        )[0]
        assert p.consumed_ratio is None
        assert p.sr_hold_rate is None

    def test_sr_test_hold_and_break(self):
        t0 = 1_700_000_000
        # bid 墙 price_low=99000：K 线触及但收盘守住 → partial（守住）
        recs = [_rec("held", t0, price_mid=99_100.0)]
        klines_hold = [KlinePoint(ts=t0 + 60, open=99_500, high=99_600,
                                  low=98_900, close=99_400)]
        p = compute_zone_band_profiles(recs, [], klines_hold)[0]
        assert p.sr_tests == 1 and p.sr_holds == 1
        assert p.sr_hold_rate == pytest.approx(1.0)

        # 收盘跌破 price_low → hit（测试失败）
        klines_break = [KlinePoint(ts=t0 + 60, open=99_500, high=99_600,
                                   low=98_000, close=98_500)]
        p2 = compute_zone_band_profiles(recs, [], klines_break)[0]
        assert p2.sr_tests == 1 and p2.sr_holds == 0
        assert p2.sr_hold_rate == pytest.approx(0.0)

        # 从未触及 → 不算测试
        klines_far = [KlinePoint(ts=t0 + 60, open=99_500, high=99_600,
                                 low=99_300, close=99_400)]
        p3 = compute_zone_band_profiles(recs, [], klines_far)[0]
        assert p3.sr_tests == 0
        assert p3.sr_hold_rate is None


class TestRuntimeProfile:

    def setup_method(self):
        whp.reset_for_test()

    def teardown_method(self):
        whp.reset_for_test()

    def _write_archive(self, root, coin, day_key, rows, *, gz=False):
        d = root / coin
        d.mkdir(parents=True, exist_ok=True)
        blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        if gz:
            with gzip.open(d / f"{day_key}.jsonl.gz", "wt", encoding="utf-8") as f:
                f.write(blob)
        else:
            (d / f"{day_key}.jsonl").write_text(blob, encoding="utf-8")

    def _snapshot_row(self, ts, zid="aaa"):
        return {
            "ts": ts, "coin": "BTC", "last_price": 99_500.0,
            "walls_above": [],
            "walls_below": [{
                "wall_zone_id": zid, "side": "bid",
                "price_low": 99_000, "price_high": 99_200, "price_mid": 99_100,
                "current_usd": 1_000_000, "trust_score": 0.6,
            }],
            "wall_events": [{
                "wall_zone_id": zid, "event_type": "wall_consumed",
                "side": "bid", "price_mid": 99_100, "ts": ts, "confidence": 0.7,
            }],
        }

    def test_refresh_and_attach(self, tmp_path):
        import datetime
        now = int(time.time())
        # 今天（北京时区）的日键 + 昨天的 gz 键
        tz = whp._TZ_CN
        today = datetime.datetime.fromtimestamp(now, tz=tz).strftime("%Y%m%d")
        yesterday = datetime.datetime.fromtimestamp(
            now - 86400, tz=tz).strftime("%Y%m%d")

        # 用小时对齐的时间戳，保证三帧落在 3 个确定不同的小时桶
        hour0 = now - (now % 3600)
        self._write_archive(tmp_path, "BTC", today,
                            [self._snapshot_row(hour0 - 3600 + 10),
                             self._snapshot_row(hour0 - 7200 + 10)])
        self._write_archive(tmp_path, "BTC", yesterday,
                            [self._snapshot_row(now - 86400 + 3600)], gz=True)

        n = whp.refresh_profile("BTC", history_root=str(tmp_path), now=now)
        assert n == 1
        meta = whp.get_profile_meta("BTC")
        assert meta is not None and meta["zones"] == 1
        assert meta["skipped_files"] == 0

        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.4, current_usd=1_000_000,
            max_usd_1h=1_000_000, avg_usd_1h=900_000, bin_count=1,
            seen_count=3, visible_minutes=15, persistence_score=0.3,
        )
        whp.attach_history_profile("BTC", [z])
        # 3 个不同小时桶 / 168h
        assert z.history_presence_7d == pytest.approx(3 / 168, abs=0.002)
        # 只有 consumed 事件 → 兑现率 1.0
        assert z.history_consumed_ratio == pytest.approx(1.0)

    def test_attach_tolerates_bucket_drift(self, tmp_path):
        # zone 价格与归档价差 ~0.15%（不足一个 0.2% 桶但可能跨桶边界）
        # → 邻桶（idx±1）命中兜底
        now = int(time.time())
        import datetime
        today = datetime.datetime.fromtimestamp(
            now, tz=whp._TZ_CN).strftime("%Y%m%d")
        self._write_archive(tmp_path, "BTC", today,
                            [self._snapshot_row(now - 600)])
        whp.refresh_profile("BTC", history_root=str(tmp_path), now=now)

        z = WallZone(
            side="bid", price_low=99_000, price_high=99_400,
            price_mid=99_250,   # 与归档 99_100 差 0.15%
            peak_price=99_250, distance_pct=-0.3, current_usd=1_000_000,
            max_usd_1h=1_000_000, avg_usd_1h=900_000, bin_count=1,
            seen_count=3, visible_minutes=15, persistence_score=0.3,
        )
        whp.attach_history_profile("BTC", [z])
        assert z.history_presence_7d is not None

    def test_oversized_file_skipped(self, tmp_path):
        # 构造一个解压后超限的 gz（伪造：真写一个 > 上限的稀疏文件太慢，
        # 这里临时调低上限验证跳过路径）
        now = int(time.time())
        import datetime
        today = datetime.datetime.fromtimestamp(
            now, tz=whp._TZ_CN).strftime("%Y%m%d")
        yesterday = datetime.datetime.fromtimestamp(
            now - 86400, tz=whp._TZ_CN).strftime("%Y%m%d")
        # 昨天：一个"大"文件（多行）；今天：正常小文件
        big_rows = [self._snapshot_row(now - 86400 + i) for i in range(50)]
        self._write_archive(tmp_path, "BTC", yesterday, big_rows, gz=True)
        self._write_archive(tmp_path, "BTC", today,
                            [self._snapshot_row(now - 600)])

        orig = whp._MAX_DAY_UNCOMPRESSED_BYTES
        whp._MAX_DAY_UNCOMPRESSED_BYTES = 1024   # 1KB，昨天的必超限
        try:
            n = whp.refresh_profile("BTC", history_root=str(tmp_path), now=now)
        finally:
            whp._MAX_DAY_UNCOMPRESSED_BYTES = orig
        assert n == 1
        meta = whp.get_profile_meta("BTC")
        assert meta["skipped_files"] == 1
        # 只有今天 1 帧 1 个小时桶
        table = whp._PROFILE_CACHE["BTC"]
        assert list(table.values())[0]["presence_7d"] == pytest.approx(
            1 / 168, abs=0.002)

    def test_missing_archive_keeps_none(self, tmp_path):
        n = whp.refresh_profile("BTC", history_root=str(tmp_path))
        assert n == 0
        z = WallZone(
            side="bid", price_low=99_000, price_high=99_200, price_mid=99_100,
            peak_price=99_100, distance_pct=-0.4, current_usd=1_000_000,
            max_usd_1h=1_000_000, avg_usd_1h=900_000, bin_count=1,
            seen_count=3, visible_minutes=15, persistence_score=0.3,
        )
        whp.attach_history_profile("BTC", [z])
        assert z.history_presence_7d is None
        assert z.history_consumed_ratio is None
