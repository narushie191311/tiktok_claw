"""twikit ベースの Twitter/X スクレイピングコレクター.

APIキー不要で Twitter 検索を行う。Twitter 側の変更で不安定になる
リスクがあるため、tweepy (公式API) へのフォールバックを前提とする。
"""

from __future__ import annotations

import os
from datetime import datetime

from Iran_ocint.collectors.base import AbstractCollector, CollectedPost
from Iran_ocint.utils.logger import get_logger
from Iran_ocint.utils.rate_limiter import twitter_scraper_limiter

log = get_logger(__name__)


class TwitterScraperCollector(AbstractCollector):
    """twikit を使った Twitter スクレイピングコレクター.

    認証には Twitter の Cookie 情報 (ct0, auth_token) を使用する。
    環境変数 TWIKIT_CT0 / TWIKIT_AUTH_TOKEN で設定。

    Attributes:
        _client: twikit の Client インスタンス.
        _initialized: 初期化済みフラグ.
    """

    def __init__(self) -> None:
        """コンストラクタ."""
        self._client = None
        self._initialized = False

    async def initialize(self) -> None:
        """twikit クライアントを初期化し Cookie 認証を行う.

        Raises:
            ImportError: twikit がインストールされていない場合.
            ConnectionError: Cookie 情報が不足している場合.
        """
        try:
            from twikit import Client
        except ImportError:
            log.error("twikit_not_installed", hint="pip install twikit")
            raise ImportError("twikit is required. Install with: pip install twikit")

        ct0 = os.getenv("TWIKIT_CT0", "")
        auth_token = os.getenv("TWIKIT_AUTH_TOKEN", "")

        if not ct0 or not auth_token:
            log.warning(
                "twikit_cookies_missing",
                hint="Set TWIKIT_CT0 and TWIKIT_AUTH_TOKEN in .env",
            )
            self._client = Client(language="en-US")
            self._initialized = False
            return

        self._client = Client(language="en-US")
        self._client.set_cookies({"ct0": ct0, "auth_token": auth_token})
        self._initialized = True
        log.info("twikit_collector_initialized")

    async def collect(
        self, queries: list[str], max_results: int = 100
    ) -> list[CollectedPost]:
        """Twitter 検索を実行しツイートを収集する.

        Args:
            queries: 検索クエリ文字列のリスト.
            max_results: クエリあたりの最大取得件数.

        Returns:
            CollectedPost のリスト. 認証未完了時は空リスト.
        """
        if not self._initialized or self._client is None:
            log.warning("twikit_not_ready", msg="Skipping collection — not authenticated")
            return []

        all_posts: list[CollectedPost] = []

        for query in queries:
            try:
                async with twitter_scraper_limiter:
                    tweets = await self._client.search_tweet(
                        query, product="Latest", count=min(max_results, 20)
                    )

                for tweet in tweets:
                    post = CollectedPost(
                        post_id=str(tweet.id),
                        author_handle=tweet.user.screen_name if tweet.user else "unknown",
                        author_name=tweet.user.name if tweet.user else "Unknown",
                        text=tweet.text or "",
                        lang=tweet.lang or "und",
                        url=f"https://x.com/{tweet.user.screen_name}/status/{tweet.id}"
                        if tweet.user
                        else "",
                        source="twikit",
                        retweet_count=tweet.retweet_count or 0,
                        like_count=tweet.favorite_count or 0,
                        reply_count=getattr(tweet, "reply_count", 0) or 0,
                        posted_at=_parse_twitter_date(tweet.created_at),
                    )
                    all_posts.append(post)

                log.info(
                    "twikit_query_done",
                    query=query,
                    count=len(tweets) if tweets else 0,
                )

            except Exception as exc:
                log.error("twikit_query_failed", query=query, error=str(exc))
                continue

        return all_posts

    async def shutdown(self) -> None:
        """twikit クライアントをクリーンアップする."""
        self._client = None
        self._initialized = False
        log.info("twikit_collector_shutdown")


def _parse_twitter_date(date_str: str | None) -> datetime | None:
    """Twitter の日付文字列をパースする.

    Args:
        date_str: "Wed Oct 10 20:19:24 +0000 2018" 形式の文字列.

    Returns:
        datetime インスタンス. パース失敗時は None.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None
