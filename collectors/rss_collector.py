"""RSS/Atom フィードコレクター.

feedparser を使ってニュースフィードからイラン関連記事を収集する。
Twitter が利用不能な場合の補完ソースとして機能する。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from time import mktime

from dataclasses import dataclass

import aiohttp
import feedparser

from collectors.base import AbstractCollector, CollectedPost
from utils.logger import get_logger
from utils.rate_limiter import rss_limiter

log = get_logger(__name__)


@dataclass
class FeedConfig:
    """RSS フィード設定.

    Attributes:
        url: フィードURL.
        name: フィード表示名.
        lang: フィードの主要言語.
    """

    url: str
    name: str
    lang: str = "en"


class RSSCollector(AbstractCollector):
    """RSS/Atom フィードからイラン関連ニュースを収集するコレクター.

    Attributes:
        _feeds: 監視対象フィード設定リスト.
        _session: aiohttp セッション.
        _seen_ids: 重複排除用の既出ID集合.
    """

    def __init__(self, feeds: list[FeedConfig] | None = None) -> None:
        """コンストラクタ.

        Args:
            feeds: 監視対象フィードリスト. None ならデフォルトフィードを使用.
        """
        self._feeds = feeds or []
        self._session: aiohttp.ClientSession | None = None
        self._seen_ids: set[str] = set()

    async def initialize(self) -> None:
        """HTTP セッションを初期化する."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Iran_ocint/0.1 RSS Reader"},
        )
        log.info("rss_collector_initialized", feed_count=len(self._feeds))

    async def collect(
        self, queries: list[str], max_results: int = 100
    ) -> list[CollectedPost]:
        """全登録フィードを巡回し、クエリにマッチする記事を収集する.

        Args:
            queries: フィルタ用キーワードリスト. 記事タイトル/本文に含まれるか判定.
            max_results: フィードあたりの最大取得件数.

        Returns:
            CollectedPost のリスト.
        """
        if self._session is None:
            await self.initialize()

        all_posts: list[CollectedPost] = []
        query_lower = {q.lower() for q in queries}

        for feed_cfg in self._feeds:
            try:
                async with rss_limiter:
                    posts = await self._fetch_feed(feed_cfg, query_lower, max_results)
                    all_posts.extend(posts)
            except Exception as exc:
                log.error("rss_feed_failed", feed=feed_cfg.name, error=str(exc))
                continue

        log.info("rss_collection_done", total=len(all_posts))
        return all_posts

    async def _fetch_feed(
        self,
        feed_cfg: FeedConfig,
        query_terms: set[str],
        max_results: int,
    ) -> list[CollectedPost]:
        """単一フィードからデータを取得しフィルタする.

        Args:
            feed_cfg: フィード設定.
            query_terms: フィルタ用の小文字キーワード集合.
            max_results: 最大件数.

        Returns:
            マッチした CollectedPost のリスト.
        """
        assert self._session is not None

        async with self._session.get(feed_cfg.url) as resp:
            if resp.status != 200:
                log.warning("rss_http_error", feed=feed_cfg.name, status=resp.status)
                return []
            raw = await resp.text()

        parsed = feedparser.parse(raw)
        posts: list[CollectedPost] = []

        for entry in parsed.entries[:max_results]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            content_text = f"{title} {summary}".lower()

            # クエリキーワードでフィルタ (空の場合は全件通過)
            if query_terms and not any(q in content_text for q in query_terms):
                continue

            entry_id = _make_entry_id(entry)
            if entry_id in self._seen_ids:
                continue
            self._seen_ids.add(entry_id)

            post = CollectedPost(
                post_id=entry_id,
                author_handle=feed_cfg.name,
                author_name=entry.get("author", feed_cfg.name),
                text=f"{title}\n\n{summary}".strip(),
                lang=feed_cfg.lang,
                url=entry.get("link", ""),
                source="rss",
                posted_at=_parse_feed_date(entry),
            )
            posts.append(post)

        log.info("rss_feed_done", feed=feed_cfg.name, matched=len(posts))
        return posts

    async def shutdown(self) -> None:
        """HTTP セッションを閉じる."""
        if self._session:
            await self._session.close()
            self._session = None
        log.info("rss_collector_shutdown")


def _make_entry_id(entry: dict) -> str:
    """フィードエントリの一意IDを生成する.

    Args:
        entry: feedparser のエントリ辞書.

    Returns:
        SHA256 ハッシュベースの一意ID文字列.
    """
    raw_id = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw_id.encode()).hexdigest()[:32]


def _parse_feed_date(entry: dict) -> datetime | None:
    """フィードエントリの日付をパースする.

    Args:
        entry: feedparser のエントリ辞書.

    Returns:
        datetime インスタンス. パース失敗時は None.
    """
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if published:
        try:
            return datetime.fromtimestamp(mktime(published))
        except (ValueError, OverflowError, OSError):
            return None
    return None
