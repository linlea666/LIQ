"""OKX Timeline 新闻源实现（D07）

覆盖两个接口：
  - 行业资讯：queryName=822539543374725120  (size=50)
  - 博主动态：queryName=789451205264277506  (size=20)

基础 URL:
  https://www.okx.com/priapi/v5/content/public/timeline/1
    ?size={N}&type=3&queryName={QUERY_NAME}

原始字段（已确认）：
  data[].contentId          → external_id
  data[].publishTime (ms)   → publish_time
  data[].titleNew / title   → title
  data[].contentCnShort     → content (中文摘要)
  data[].contentEnShort     → translated_content
  data[].viewCount / likeCount / commentCount → heat_score 归一化
  data[].hashTagList / cashTagList  → raw_tags
  data[].shareUrl           → share_url
  data[].nickName           → source_author（博主接口更有意义）

落实日志锚点：
  - D.D07_NEWS_SOURCES：每次 fetch 上报 items / dedupe_dropped / latency_ms
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Optional

import aiohttp

from models.news_event import RawNewsItem
from sources.news.base import NewsSource

logger = logging.getLogger(__name__)


_BASE_URL = "https://www.okx.com/priapi/v5/content/public/timeline/1"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class OkxTimelineSource(NewsSource):
    """OKX 新闻/博主 timeline 源"""

    source_type: str = "okx"

    def __init__(
        self,
        name: str,
        query_name: str,
        size: int = 50,
        source_author: str = "OKX",
        source_reliability: float = 0.8,
        poll_interval_min: int = 15,
        timeout_sec: int = 10,
    ) -> None:
        super().__init__(name=name)
        self.query_name = query_name
        self.size = size
        self.source_author = source_author
        self.source_reliability = source_reliability
        self.poll_interval_min = poll_interval_min
        self.timeout_sec = timeout_sec

    # ── 构造 URL（公开便于测试） ──
    def build_url(self) -> str:
        """拼接请求 URL"""
        return f"{_BASE_URL}?size={self.size}&type=3&queryName={self.query_name}"

    # ── 主 fetch ──
    async def fetch(self, since_ts: Optional[int] = None) -> list[RawNewsItem]:
        """拉取并解析 OKX timeline.

        流程：
          1. GET build_url() · 带 UA 头
          2. 解析 data[].* 字段 → RawNewsItem
          3. since_ts 过滤（publish_time 毫秒 → 秒比较）
          4. filter_new() 内存去重（由 registry 间接调用或本方法末尾调用）
          5. 按 publish_time 升序
          6. _mark_fetched() 更新时间戳
        """
        url = self.build_url()
        t0 = time.time()
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[D07] %s fetch failed: %s", self.name, e,
            )
            raise

        # 兼容两种响应外壳：
        #   旧形态：{"data": [ {...}, ... ]}
        #   新形态：{"data": {"contentDataList": [...], "nextCursor": "..."}}
        # 2026-04 实测 OKX 已切到新形态；这里做双兼容避免未来再翻车。
        data_obj = (payload or {}).get("data")
        if isinstance(data_obj, list):
            raw_list: list[Any] = data_obj
        elif isinstance(data_obj, dict):
            raw_list = (
                data_obj.get("contentDataList")
                or data_obj.get("items")
                or data_obj.get("list")
                or []
            )
        else:
            raw_list = []
        if not isinstance(raw_list, list):
            logger.warning(
                "[D07] %s unexpected data shape payload_keys=%s data_type=%s",
                self.name,
                list((payload or {}).keys())[:8],
                type(data_obj).__name__,
            )
            raw_list = []

        items: list[RawNewsItem] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            try:
                item = self._normalize(raw)
            except Exception:  # noqa: BLE001
                logger.debug("[D07] %s normalize failed for raw=%r", self.name, raw, exc_info=True)
                continue
            if item is None:
                continue

            # since_ts 过滤（publish_time 是毫秒）
            if since_ts is not None and item.publish_time > 0:
                if (item.publish_time // 1000) < int(since_ts):
                    continue
            items.append(item)

        # 升序排序（AI/下游默认按时间顺序处理）
        items.sort(key=lambda it: it.publish_time)

        latency_ms = int((time.time() - t0) * 1000)
        self._mark_fetched(int(time.time()))

        logger.info(
            "[D07] %s fetched | total=%d since_ts=%s latency_ms=%d",
            self.name, len(items), since_ts, latency_ms,
        )
        return items

    # ── 字段归一化（公开便于单测） ──
    def _normalize(self, raw: dict[str, Any]) -> Optional[RawNewsItem]:
        """把单条 OKX 原始 dict 转成 RawNewsItem

        要点：
          - publish_time 保留毫秒（模型字段约定）
          - heat_score = normalize(viewCount/likeCount) 0-1
          - lang 按 OKX 主站语言推断（zh 优先）
          - raw_tags 合并 cashTagList + hashTagList
          - share_url 直接透传
          - raw_payload 保留完整原始 dict（debug）
        """
        external_id = str(raw.get("contentId") or raw.get("id") or "").strip()
        if not external_id:
            return None

        publish_time = _to_int(raw.get("publishTime") or raw.get("publish_time"))
        # 有些条目返回 created_at 秒级，兼容一下（< 1e12 视为秒 → 转毫秒）
        if 0 < publish_time < 1_000_000_000_000:
            publish_time *= 1000

        title = str(
            raw.get("titleNew") or raw.get("title") or raw.get("mainContent") or ""
        ).strip()

        content_cn = str(
            raw.get("contentCnShort") or raw.get("contentShort") or raw.get("content") or ""
        ).strip()

        translated_title = str(
            raw.get("titleEn") or raw.get("translatedTitle") or ""
        ).strip()
        translated_content = str(
            raw.get("contentEnShort") or raw.get("translatedContent") or ""
        ).strip()

        view_count = _to_int(raw.get("viewCount") or raw.get("readCount"))
        like_count = _to_int(raw.get("likeCount"))
        comment_count = _to_int(raw.get("commentCount"))
        heat = self._heat_score(view_count, like_count, comment_count)

        raw_tags: list[str] = []
        for tag_key in ("cashTagList", "hashTagList", "tokens", "tags"):
            tag_items = raw.get(tag_key) or []
            if isinstance(tag_items, list):
                for t in tag_items:
                    if isinstance(t, str):
                        tag_v = t.strip()
                    elif isinstance(t, dict):
                        tag_v = str(t.get("tag") or t.get("name") or t.get("code") or "").strip()
                    else:
                        tag_v = ""
                    if tag_v and tag_v not in raw_tags:
                        raw_tags.append(tag_v)

        nick_name = str(raw.get("nickName") or "").strip()
        author = nick_name or self.source_author

        share_url = str(
            raw.get("shareUrl") or raw.get("share_url") or raw.get("jumpUrl") or ""
        ).strip()

        # 语言推断：有中文字段则 zh；否则 en
        lang = "zh" if (content_cn or _has_cjk(title)) else "en"

        return RawNewsItem(
            source_type=self.source_type,
            source_author=author,
            source_reliability=self.source_reliability,
            external_id=external_id,
            publish_time=publish_time,
            fetch_time=int(time.time()),
            title=title,
            content=content_cn,
            lang=lang,
            translated_title=translated_title,
            translated_content=translated_content,
            heat_score=heat,
            view_count=view_count,
            like_count=like_count,
            comment_count=comment_count,
            raw_tags=raw_tags,
            share_url=share_url,
            raw_payload=raw,
        )

    # ── 热度归一化 ──
    @staticmethod
    def _heat_score(view_count: int, like_count: int, comment_count: int) -> float:
        """综合打分 → 0-1。log-scale 三值加权。

        公式：
          s = 0.5*log10(1+view) + 0.3*log10(1+like) + 0.2*log10(1+comment)
          clip(s / 4.5, 0, 1)  # 经验上限（view=100k 时约 0.55）
        """
        try:
            v = max(0, int(view_count or 0))
            l = max(0, int(like_count or 0))
            c = max(0, int(comment_count or 0))
        except Exception:  # noqa: BLE001
            return 0.0
        if v == 0 and l == 0 and c == 0:
            return 0.0
        score = (
            0.5 * math.log10(1 + v)
            + 0.3 * math.log10(1 + l)
            + 0.2 * math.log10(1 + c)
        )
        norm = score / 4.5
        return max(0.0, min(1.0, round(norm, 4)))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _to_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        return int(str(v).strip() or 0)
    except (ValueError, TypeError):
        return 0


def _has_cjk(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 快捷构造函数（供 registry.load_from_yaml 调用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_industry_source() -> OkxTimelineSource:
    """OKX 行业资讯源（queryName=822539543374725120）"""
    return OkxTimelineSource(
        name="okx_industry",
        query_name="822539543374725120",
        size=50,
        source_author="OKX-Industry",
        source_reliability=0.85,
        poll_interval_min=15,
    )


def create_kol_source() -> OkxTimelineSource:
    """OKX 博主动态源（queryName=789451205264277506）"""
    return OkxTimelineSource(
        name="okx_kol",
        query_name="789451205264277506",
        size=20,
        source_author="OKX-KOL",
        source_reliability=0.70,
        poll_interval_min=30,
    )
