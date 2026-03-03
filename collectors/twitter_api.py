"""tweepy ベースの Twitter/X 公式 API v2 コレクター.

Twitter API v2 の Bearer Token 認証を使用する安定版コレクター。
Basic ($100/月) 以上のプランが必要。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from Iran_ocint.collectors.base import AbstractCollector, CollectedPost
from Iran_ocint.utils.logger import get_logger
from Iran_ocint.utils.rate_limiter import twitter_api_limiter

log = get_logger(__name__)


class TwitterAPICollector(AbstractCollector):
    """tweepy を使った Twitter API v2 コレクター.

    環境変数 TWITTER_BEARER_TOKEN で認証。

    Attributes:
        _client: tweepy.Client インスタンス.
        _initialized: 初期化済みフラグ.
    """

    def __init__(self) -> None:
        """コンストラクタ."""
        self._client = None
        self._initialized = False

    async def initialize(self) -> None:
        """tweepy クライアントを初期化する.

        Raises:
            ImportError: tweepy がインストールされていない場合.
        """
        try:
            import tweepy
        except ImportError:
            log.error("tweepy_not_installed", hint="pip install tweepy")
            raise ImportError("tweepy is required. Install with: pip install tweepy")

        bearer_token = os.getenv("TWITTER_BEARER_TOKEN", "")
        if not bearer_token:
            log.warning(
                "twitter_bearer_missing",
                hint="Set TWITTER_BEARER_TOKEN in .env for API v2 access",
            )
            self._initialized = False
            return

        self._client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)
        self._initialized = True
        log.info("tweepy_collector_initialized")

    async def collect(
        self, queries: list[str], max_results: int = 100
    ) -> list[CollectedPost]:
        """Twitter API v2 で検索を実行しツイートを収集する.

        Args:
            queries: 検索クエリ文字列のリスト.
            max_results: クエリあたりの最大取得件数 (10~100).

        Returns:
            CollectedPost のリスト. API 未設定時は空リスト.
        """
        if not self._initialized or self._client is None:
            log.warning("tweepy_not_ready", msg="Skipping — no bearer token")
            return []

        all_posts: list[CollectedPost] = []
        clamped_max = max(10, min(max_results, 100))

        for query in queries:
            try:
                async with twitter_api_limiter:
                    # tweepy v2 search_recent_tweets は同期呼び出し
                    response = self._client.search_recent_tweets(
                        query=query,
                        max_results=clamped_max,
                        tweet_fields=["created_at", "lang", "public_metrics", "author_id"],
                        user_fields=["username", "name"],
                        expansions=["author_id"],
                    )

                if not response.data:
                    log.info("tweepy_no_results", query=query)
                    continue

                # author_id → User のマッピングを構築
                users_map: dict[str, dict] = {}
                if response.includes and "users" in response.includes:
                    for user in response.includes["users"]:
                        users_map[str(user.id)] = {
                            "username": user.username,
                            "name": user.name,
                        }

                for tweet in response.data:
                    author = users_map.get(str(tweet.author_id), {})
                    handle = author.get("username", "unknown")
                    metrics = tweet.public_metrics or {}

                    post = CollectedPost(
                        post_id=str(tweet.id),
                        author_handle=handle,
                        author_name=author.get("name", "Unknown"),
                        text=tweet.text or "",
                        lang=tweet.lang or "und",
                        url=f"https://x.com/{handle}/status/{tweet.id}",
                        source="tweepy",
                        retweet_count=metrics.get("retweet_count", 0),
                        like_count=metrics.get("like_count", 0),
                        reply_count=metrics.get("reply_count", 0),
                        posted_at=tweet.created_at.replace(tzinfo=timezone.utc)
                        if tweet.created_at
                        else None,
                    )
                    all_posts.append(post)

                log.info(
                    "tweepy_query_done",
                    query=query,
                    count=len(response.data),
                )

            except Exception as exc:
                log.error("tweepy_query_failed", query=query, error=str(exc))
                continue

        return all_posts

    async def shutdown(self) -> None:
        """tweepy クライアントをクリーンアップする."""
        self._client = None
        self._initialized = False
        log.info("tweepy_collector_shutdown")
