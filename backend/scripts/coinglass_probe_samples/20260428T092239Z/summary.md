# Coinglass Probe Summary · 2026-04-28T09:22:47+00:00

> 本报告由 `backend/scripts/probe_coinglass.py` 自动生成。
> 用途：流动性墙引擎设计调研，不进生产链路。

## 总体
- 探测条数：1
- 成功：1
- 失败：0

## P0-墙(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `orderbook_history` | ✅ | 8262.2ms | list × 12 | 5m 分价位深度热力图，OP 模块当前 limit=2，建议升级 limit=12 |

