#!/usr/bin/env python3
"""V1 存量数据一次性修复脚本。

修复两类由已知 bug 造成的历史脏数据（bug 本身已在代码中修复）：

1. 僵尸状态行：状态变更曾在落库之后才应用到内存（时序 bug），
   终局评估（如 S1→DEAD）从未写进 token_master。表现为币早已归零，
   token_master 仍停在 S1/DISTRIBUTION，重启恢复时死币被复活。
   判定标准（基于该币最新快照，保守）：
     - 流动性 < dead_min_liquidity（$500），或
     - 价格相对其快照历史最高价跌幅 ≥ 95% 且最新快照已超过 30 分钟无更新
   命中则置 DEAD。

2. 假 MOON outcome：追踪器曾把详情接口的回看极值（interval_high，
   覆盖信号**之前**的价格）灌进 raw_ath 与 horizon 窗口，
   伪造出数百倍的 peak_multiple。表现为 raw_ath_at 紧跟 signal_at
   且倍数离谱。修复方式：仅用信号之后快照的 price 列重算
   raw_ath / min_price / peak / mfe / mae / horizons / label，
   并在 horizons_json 里留下 "_repaired" 标记。

用法（在服务**停止**后、重启前执行）：
    python3 repair_v1_data.py /path/to/radar.db          # 试运行，只报告
    python3 repair_v1_data.py /path/to/radar.db --apply  # 实际写入

只依赖标准库，可直接在服务器宿主机或容器内运行。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

DEAD_MIN_LIQUIDITY = 500.0
PRICE_COLLAPSE_RATIO = 0.05     # 最新价 <= 历史最高价的 5%（即跌幅 >= 95%）
STALE_SNAPSHOT_MS = 30 * 60_000  # 价格崩塌判死额外要求快照已停更 30 分钟
FAKE_ATH_WINDOW_MS = 60_000     # raw_ath 在信号后 60 秒内出现
FAKE_PEAK_MULTIPLE = 10.0       # 且倍数 >= 10 → 判定为回看极值污染

ACTIVE_STATES = ("S0", "S1", "S2", "MOMENTUM", "DISTRIBUTION")


def now_ms() -> int:
    return int(time.time() * 1000)


# ═════════════════════════════════════════════════════════════════════════
# 1. 僵尸状态
# ═════════════════════════════════════════════════════════════════════════

def find_zombie_tokens(conn: sqlite3.Connection) -> list[dict]:
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    rows = conn.execute(
        f"SELECT token_id, symbol, state, state_since_ms FROM token_master "
        f"WHERE state IN ({placeholders})",
        ACTIVE_STATES,
    ).fetchall()

    zombies: list[dict] = []
    for row in rows:
        token_id = row["token_id"]
        latest = conn.execute(
            "SELECT observed_at, price, liquidity FROM snapshots "
            "WHERE token_id=? ORDER BY observed_at DESC LIMIT 1",
            (token_id,),
        ).fetchone()
        if latest is None:
            continue

        reason = None
        liquidity = latest["liquidity"]
        price = latest["price"]
        if liquidity is not None and liquidity < DEAD_MIN_LIQUIDITY:
            reason = f"流动性 ${liquidity:,.0f} < ${DEAD_MIN_LIQUIDITY:,.0f}"
        elif price is not None and price > 0:
            peak = conn.execute(
                "SELECT MAX(price) AS p FROM snapshots WHERE token_id=?",
                (token_id,),
            ).fetchone()["p"]
            stale = now_ms() - int(latest["observed_at"]) >= STALE_SNAPSHOT_MS
            if peak and price <= peak * PRICE_COLLAPSE_RATIO and stale:
                reason = (
                    f"价格 {price:.3e} 较历史最高 {peak:.3e} "
                    f"跌幅 {(1 - price / peak) * 100:.1f}%，且已停更"
                )
        if reason:
            zombies.append({
                "token_id": token_id,
                "symbol": row["symbol"],
                "state": row["state"],
                "observed_at": int(latest["observed_at"]),
                "reason": reason,
            })
    return zombies


def repair_zombies(conn: sqlite3.Connection, zombies: list[dict]) -> None:
    for z in zombies:
        conn.execute(
            "UPDATE token_master SET state='DEAD', state_since_ms=? "
            "WHERE token_id=?",
            (z["observed_at"], z["token_id"]),
        )


# ═════════════════════════════════════════════════════════════════════════
# 2. 假 MOON outcome
# ═════════════════════════════════════════════════════════════════════════

def find_fake_outcomes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT o.alert_id, o.token_id, o.signal_at, o.signal_price, "
        "o.raw_ath_price, o.raw_ath_at, o.peak_multiple, o.outcome_label, "
        "o.horizons_json, t.symbol "
        "FROM outcomes o JOIN token_master t ON t.token_id = o.token_id "
        "WHERE o.raw_ath_at IS NOT NULL AND o.peak_multiple IS NOT NULL "
        "AND o.raw_ath_at - o.signal_at < ? AND o.peak_multiple >= ?",
        (FAKE_ATH_WINDOW_MS, FAKE_PEAK_MULTIPLE),
    ).fetchall()
    return [dict(r) for r in rows]


def _label_for(peak: float | None, mae_pct: float | None) -> str | None:
    """与 radar.tracker._label_for 保持一致的粗粒度标签。"""
    if peak is None:
        return None
    if mae_pct is not None and mae_pct <= -80.0:
        return "RUG"
    if peak >= 10.0:
        return "MOON"
    if peak >= 5.0:
        return "STRONG"
    if peak >= 2.0:
        return "WIN"
    if peak >= 1.2:
        return "SMALL_WIN"
    return "FLAT"


def recompute_outcome(conn: sqlite3.Connection, outcome: dict) -> dict | None:
    """仅用信号之后快照的 price 列重算极值。

    不再信任 interval_high/low（污染源）。快照采样虽稀疏，
    但"诚实的低估"优于"伪造的高估"。
    """
    signal_at = int(outcome["signal_at"])
    signal_price = outcome["signal_price"]
    rows = conn.execute(
        "SELECT observed_at, price FROM snapshots "
        "WHERE token_id=? AND observed_at >= ? AND price IS NOT NULL "
        "AND price > 0 ORDER BY observed_at ASC",
        (outcome["token_id"], signal_at),
    ).fetchall()

    prices = [(int(r["observed_at"]), float(r["price"])) for r in rows]
    if not prices:
        # 信号后没有任何可信价格点：极值全部置 NULL（诚实的"不知道"）
        return {
            "raw_ath_price": None, "raw_ath_mc": None, "raw_ath_at": None,
            "min_price": None, "min_price_at": None,
            "peak_multiple": None, "mfe_pct": None, "mae_pct": None,
            "outcome_label": None, "horizons": None,
        }

    ath_at, ath = max(prices, key=lambda p: p[1])
    low_at, low = min(prices, key=lambda p: p[1])

    peak = mfe = mae = None
    if signal_price and signal_price > 0:
        peak = round(ath / signal_price, 4)
        mfe = round((ath / signal_price - 1.0) * 100.0, 2)
        mae = round((low / signal_price - 1.0) * 100.0, 2)

    # 重算 horizon 窗口（沿用原 JSON 里的窗口标签）
    horizons = None
    old = outcome.get("horizons_json")
    if old:
        try:
            horizons = {}
            for label, payload in json.loads(old).items():
                if label.startswith("_"):
                    continue
                horizon_ms = _label_to_ms(label)
                in_window = [p for t, p in prices if t - signal_at <= horizon_ms]
                w_mfe = w_mae = None
                if in_window and signal_price and signal_price > 0:
                    w_mfe = round((max(in_window) / signal_price - 1.0) * 100.0, 2)
                    w_mae = round((min(in_window) / signal_price - 1.0) * 100.0, 2)
                horizons[label] = {
                    "mfe_pct": w_mfe,
                    "mae_pct": w_mae,
                    "matured": bool(payload.get("matured"))
                    or now_ms() - signal_at > horizon_ms,
                }
            horizons["_repaired"] = "lookback_ath_pollution"
        except (ValueError, TypeError):
            horizons = None

    return {
        "raw_ath_price": ath, "raw_ath_mc": None, "raw_ath_at": ath_at,
        "min_price": low, "min_price_at": low_at,
        "peak_multiple": peak, "mfe_pct": mfe, "mae_pct": mae,
        "outcome_label": _label_for(peak, mae),
        "horizons": horizons,
    }


def _label_to_ms(label: str) -> int:
    label = label.strip().lower()
    if label.endswith("d"):
        return int(float(label[:-1]) * 86_400_000)
    if label.endswith("h"):
        return int(float(label[:-1]) * 3_600_000)
    return int(float(label) * 3_600_000)


def repair_outcome(conn: sqlite3.Connection, alert_id: int, fixed: dict) -> None:
    conn.execute(
        "UPDATE outcomes SET raw_ath_price=?, raw_ath_mc=?, raw_ath_at=?, "
        "min_price=?, min_price_at=?, peak_multiple=?, mfe_pct=?, mae_pct=?, "
        "outcome_label=?, horizons_json=COALESCE(?, horizons_json), "
        "sustained_ath_price=NULL, sustained_ath_mc=NULL, sustained_ath_at=NULL "
        "WHERE alert_id=?",
        (
            fixed["raw_ath_price"], fixed["raw_ath_mc"], fixed["raw_ath_at"],
            fixed["min_price"], fixed["min_price_at"], fixed["peak_multiple"],
            fixed["mfe_pct"], fixed["mae_pct"], fixed["outcome_label"],
            json.dumps(fixed["horizons"], ensure_ascii=False)
            if fixed["horizons"] else None,
            alert_id,
        ),
    )


# ═════════════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", help="radar.db 路径")
    parser.add_argument("--apply", action="store_true",
                        help="实际写入（默认试运行只报告）")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    zombies = find_zombie_tokens(conn)
    print(f"── 僵尸状态行：{len(zombies)} 条 ──")
    for z in zombies:
        print(f"  token_id={z['token_id']} {z['symbol'] or '?'} "
              f"[{z['state']}] → DEAD | {z['reason']}")

    fakes = find_fake_outcomes(conn)
    print(f"── 疑似污染 outcome：{len(fakes)} 条 ──")
    repairs: list[tuple[int, dict]] = []
    for f in fakes:
        fixed = recompute_outcome(conn, f)
        if fixed is None:
            continue
        repairs.append((f["alert_id"], fixed))
        print(f"  alert_id={f['alert_id']} {f['symbol'] or '?'} "
              f"peak {f['peak_multiple']}x/{f['outcome_label']} → "
              f"{fixed['peak_multiple']}x/{fixed['outcome_label']}")

    if not args.apply:
        print("\n试运行结束（未写入）。确认无误后加 --apply 执行。")
        return 0

    repair_zombies(conn, zombies)
    for alert_id, fixed in repairs:
        repair_outcome(conn, alert_id, fixed)
    conn.commit()
    conn.close()
    print(f"\n已写入：{len(zombies)} 条状态修复，{len(repairs)} 条 outcome 修复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
