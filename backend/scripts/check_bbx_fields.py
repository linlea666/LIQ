"""一次性诊断脚本：实测 BBX 接口返回内容，排查 25/33 字段覆盖率缺口

用法：
  cd backend && python3 scripts/check_bbx_fields.py

输出：
  - 每个配置字段的 key + 在响应里的命中情况（存在/缺失）+ 具体的 last/change 原值
  - missing 集合 + 所属分组（_DIRECT_MAP / _CHANGE_PCT_MAP / 汇总）
  - 便于判断：是 BBX 服务端返回为空？还是 key 变了？还是 value='-' 被判为 None？
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.bbx import BBXSource
from polls.bbx_index import _CHANGE_PCT_MAP, _DIRECT_MAP
from models.flow import MarketIndexData


async def main():
    bbx = BBXSource(cache_ttl=0)
    try:
        await bbx.fetch_all()
        if not bbx._cache:
            print("❌ BBX fetch 返回空，请检查网络/endpoint")
            return

        print(f"✅ BBX 缓存索引数量: {len(bbx._cache)}\n")

        print("=" * 80)
        print("§1. _DIRECT_MAP 字段诊断（用于 last 值取数）")
        print("=" * 80)
        direct_missing: list[tuple[str, str, str]] = []
        for bbx_key, mi_field in _DIRECT_MAP:
            item = bbx._cache.get(bbx_key)
            if item is None:
                direct_missing.append((bbx_key, mi_field, "KEY_NOT_IN_RESPONSE"))
                print(f"  ❌ {bbx_key:38s} → {mi_field:25s} | KEY 未出现在响应里")
                continue
            raw_last = item.get("last")
            val = bbx.get_float(bbx_key)
            if val is None:
                reason = f"last={raw_last!r}"
                direct_missing.append((bbx_key, mi_field, reason))
                print(f"  ⚠️  {bbx_key:38s} → {mi_field:25s} | last={raw_last!r} 被判为 None")
            else:
                print(f"  ✅ {bbx_key:38s} → {mi_field:25s} | last={val}")

        print()
        print("=" * 80)
        print("§2. _CHANGE_PCT_MAP 字段诊断（用于变化百分比）")
        print("=" * 80)
        chg_missing: list[tuple[str, str, str]] = []
        for bbx_key, mi_field in _CHANGE_PCT_MAP:
            item = bbx._cache.get(bbx_key)
            if item is None:
                chg_missing.append((bbx_key, mi_field, "KEY_NOT_IN_RESPONSE"))
                print(f"  ❌ {bbx_key:38s} → {mi_field:28s} | KEY 未出现")
                continue
            raw_last = item.get("last")
            raw_change = item.get("change")
            pct = bbx.get_change_pct(bbx_key)
            if pct is None:
                reason = f"last={raw_last!r} change={raw_change!r}"
                chg_missing.append((bbx_key, mi_field, reason))
                print(f"  ⚠️  {bbx_key:38s} → {mi_field:28s} | {reason} → pct=None")
            else:
                print(f"  ✅ {bbx_key:38s} → {mi_field:28s} | pct={pct}%")

        print()
        print("=" * 80)
        print("§3. 交易所 BTC 余额变化（exchange_btc_change_24h）")
        print("=" * 80)
        _EX_BAL_KEYS = [
            "i:bnbbtchold:arkm", "i:okxbtchold:arkm",
            "i:bitfbtchold:arkm", "i:coinbtchold:arkm",
        ]
        for k in _EX_BAL_KEYS:
            item = bbx._cache.get(k)
            if item is None:
                print(f"  ❌ {k} | KEY 未出现")
                continue
            raw_change = item.get("change")
            chg = bbx.get_change(k)
            print(f"  {'✅' if chg is not None else '⚠️ '} {k:38s} | change={raw_change!r} → {chg}")

        print()
        print("=" * 80)
        print("§4. MarketIndexData 全字段理论清单（对比 25/33 真相）")
        print("=" * 80)
        mi = MarketIndexData(ts=0)
        all_fields = [f for f in mi.__fields__ if f != "ts"]
        print(f"  MarketIndexData 共 {len(all_fields)} 个非 ts 字段")
        mapped_via_direct = {f for _, f in _DIRECT_MAP}
        mapped_via_chg = {f for _, f in _CHANGE_PCT_MAP}
        mapped = mapped_via_direct | mapped_via_chg | {"exchange_btc_change_24h"}
        unmapped = [f for f in all_fields if f not in mapped]
        if unmapped:
            print(f"  ⚠️  {len(unmapped)} 个字段没有 BBX 映射（永远是 None）:")
            for f in unmapped:
                print(f"      - {f}")
        else:
            print("  ✅ 全部字段都有映射来源")

        print()
        print("=" * 80)
        print("§5. 总结")
        print("=" * 80)
        print(f"  _DIRECT_MAP 缺失: {len(direct_missing)} / {len(_DIRECT_MAP)}")
        print(f"  _CHANGE_PCT_MAP 缺失: {len(chg_missing)} / {len(_CHANGE_PCT_MAP)}")
        print(f"  MarketIndexData 未映射字段: {len(unmapped)} / {len(all_fields)}")
        print()
        print("  → 生产日志报 25/33 覆盖率的真正缺口:")
        print(f"    · 上游 key 未返回/值为空: {len(direct_missing) + len(chg_missing)}")
        print(f"    · MarketIndexData 本身就没映射: {len(unmapped)}")

        if direct_missing:
            print()
            print("【根因清单 · _DIRECT_MAP】")
            for k, f, reason in direct_missing:
                print(f"  · {k:38s} → {f:25s} | {reason}")
        if chg_missing:
            print()
            print("【根因清单 · _CHANGE_PCT_MAP】")
            for k, f, reason in chg_missing:
                print(f"  · {k:38s} → {f:28s} | {reason}")

    finally:
        await bbx.close()


if __name__ == "__main__":
    asyncio.run(main())
