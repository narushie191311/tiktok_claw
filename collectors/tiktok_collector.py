"""TikTok トレンドコレクター.

TikTok-Api (Playwright ベース) を使い、APIキー不要で
トレンドハッシュタグ・急上昇動画の情報を収集する。

TikTok 側のサイト変更で不安定になるリスクがあるため、
エラー時はログを出して空リストを返す設計とする。

必要パッケージ:
    pip install TikTokApi playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime
from typing import Any

def _has_display() -> bool:
    """実行時点でディスプレイが利用可能か判定する (インポート時ではなく呼び出し時評価).

    Returns:
        DISPLAY または WAYLAND_DISPLAY が設定されている場合 True.
    """
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

from collectors.base import AbstractCollector, CollectedPost
from utils.logger import get_logger
from utils.rate_limiter import tiktok_limiter

log = get_logger(__name__)

_tiktok_limiter = tiktok_limiter

# ブロック回避のためのランダム遅延レンジ (秒)
_MIN_DELAY_SEC = 2.0
_MAX_DELAY_SEC = 5.0


class TikTokCollector(AbstractCollector):
    """TikTok-Api を使ったトレンド動画コレクター.

    ハッシュタグ検索・急上昇動画の収集を行う。
    環境変数 TIKTOK_COOKIE_FILE または TIKTOK_SESSION_ID で
    認証済みセッションを指定できる。

    Attributes:
        _api: TikTokApi インスタンス.
        _initialized: 初期化済みフラグ.
        _cookie_file: Playwright セッション Cookie ファイルパス.
        _ms_token: TikTok ms_token (Optional).
    """

    def __init__(self) -> None:
        """コンストラクタ."""
        self._api: Any = None
        self._initialized: bool = False
        self._cookie_file: str = os.getenv("TIKTOK_COOKIE_FILE", "")
        self._ms_token: str = os.getenv("TIKTOK_MS_TOKEN", "")

    async def initialize(self) -> None:
        """TikTokApi を初期化する.

        Playwright の chromium を内部で起動するため、
        `playwright install chromium` が必要。

        Raises:
            ImportError: TikTokApi がインストールされていない場合.
        """
        try:
            from TikTokApi import TikTokApi
        except ImportError:
            log.error(
                "tiktok_api_not_installed",
                hint="pip install TikTokApi && playwright install chromium",
            )
            raise ImportError(
                "TikTokApi is required. Install with: "
                "pip install TikTokApi && playwright install chromium"
            )

        try:
            self._api = TikTokApi()
            await self._api.__aenter__()

            # v7+ では create_sessions() でブラウザセッションを明示的に作成する。
            # DISPLAY が利用可能な場合 (Xvfb含む) は headless=False にする。
            # TikTok はヘッドレス Chromium をハッシュタグ検索でブロックするため。
            use_headless = not _has_display()
            session_kwargs: dict[str, Any] = {
                "num_sessions": 1,
                "headless": use_headless,
                "sleep_after": 3,
            }
            if self._ms_token:
                session_kwargs["ms_tokens"] = [self._ms_token]

            log.info("tiktok_creating_session", headless=use_headless, display=os.environ.get("DISPLAY", "none"), ms_token_set=bool(self._ms_token))
            await self._api.create_sessions(**session_kwargs)

            # セッション確立後に ms_token を自動取得して以後のリクエストに使用
            if self._api.sessions and not self._ms_token:
                extracted = getattr(self._api.sessions[0], "ms_token", None)
                if extracted:
                    self._ms_token = extracted
                    log.info("tiktok_ms_token_extracted", length=len(extracted))

            self._initialized = True
            log.info("tiktok_collector_initialized", ms_token_set=bool(self._ms_token))
        except Exception as exc:
            log.error("tiktok_init_failed", error=str(exc))
            self._initialized = False

    async def collect(
        self, queries: list[str], max_results: int = 30
    ) -> list[CollectedPost]:
        """TikTok ハッシュタグ検索で動画を収集する.

        queries はハッシュタグ名 (# 不要) または通常の検索語として使用。
        ハッシュタグ形式 (#xxx) の場合はハッシュタグ検索、
        それ以外はキーワード検索にルーティングする。

        Args:
            queries: 検索クエリ（ハッシュタグ名 or キーワード）のリスト.
            max_results: クエリあたりの最大取得件数 (TikTok側の制限上30推奨).

        Returns:
            CollectedPost のリスト. 未初期化・エラー時は空リスト.
        """
        if not self._initialized or self._api is None:
            log.warning("tiktok_not_ready", msg="Skipping — not initialized")
            return []

        all_posts: list[CollectedPost] = []

        for query in queries:
            try:
                posts = await self._collect_by_hashtag(query, max_results)
                all_posts.extend(posts)
                log.info("tiktok_query_done", query=query, count=len(posts))

                # ブロック回避ランダム遅延
                await asyncio.sleep(random.uniform(_MIN_DELAY_SEC, _MAX_DELAY_SEC))

            except Exception as exc:
                log.error("tiktok_query_failed", query=query, error=str(exc))
                continue

        return all_posts

    async def _collect_by_hashtag(
        self, tag: str, max_count: int
    ) -> list[CollectedPost]:
        """ハッシュタグ検索で動画を取得する.

        Args:
            tag: ハッシュタグ名 (# 有無を自動除去).
            max_count: 最大取得件数.

        Returns:
            CollectedPost のリスト.
        """
        clean_tag = tag.lstrip("#").strip()
        posts: list[CollectedPost] = []

        async with _tiktok_limiter:
            hashtag = self._api.hashtag(name=clean_tag)
            # TikTok-Api は最大 30 件/リクエストが実用的な上限
            async for video in hashtag.videos(count=min(max_count, 30)):
                post = _video_to_post(video, source_tag=clean_tag)
                if post:
                    posts.append(post)

        return posts

    async def get_trending_videos(self, count: int = 30) -> list[CollectedPost]:
        """グローバルトレンド動画を取得する.

        Args:
            count: 取得件数.

        Returns:
            CollectedPost のリスト.
        """
        if not self._initialized or self._api is None:
            return []

        posts: list[CollectedPost] = []
        try:
            async with _tiktok_limiter:
                async for video in self._api.trending.videos(count=count):
                    post = _video_to_post(video, source_tag="trending")
                    if post:
                        posts.append(post)
            log.info("tiktok_trending_collected", count=len(posts))
        except Exception as exc:
            log.error("tiktok_trending_failed", error=str(exc))

        return posts

    async def shutdown(self) -> None:
        """TikTokApi セッションをクリーンアップする."""
        if self._api is not None:
            try:
                await self._api.__aexit__(None, None, None)
            except Exception as exc:
                log.warning("tiktok_shutdown_error", error=str(exc))
            finally:
                self._api = None
        self._initialized = False
        log.info("tiktok_collector_shutdown")


def _video_to_post(video: Any, source_tag: str) -> CollectedPost | None:
    """TikTokApi v7 の Video オブジェクトを CollectedPost に変換する.

    v7 では属性は as_dict から取得し、author/create_time は直接属性として利用する。

    Args:
        video: TikTokApi の Video オブジェクト.
        source_tag: 収集元タグ名 (ログ用).

    Returns:
        CollectedPost インスタンス. 変換失敗時は None.
    """
    try:
        vid_id: str = str(video.id)

        # author は Video の直接属性
        author = video.author
        handle: str = getattr(author, "username", "") or "unknown"
        name: str = getattr(author, "nickname", handle) or handle

        # stats は dict または Video.stats 属性
        raw = getattr(video, "as_dict", {}) or {}
        stats_raw = raw.get("stats") or getattr(video, "stats", None) or {}
        like_count: int = int(stats_raw.get("diggCount", 0) or 0)
        share_count: int = int(stats_raw.get("shareCount", 0) or 0)
        comment_count: int = int(stats_raw.get("commentCount", 0) or 0)
        play_count: int = int(stats_raw.get("playCount", 0) or 0)

        # desc は as_dict 経由 (直接属性としては存在しない)
        desc: str = raw.get("desc", "") or ""

        # create_time は Video の直接属性 (v7 は datetime 型)
        create_time = getattr(video, "create_time", None)
        posted_at: datetime | None = None
        if create_time is not None:
            if isinstance(create_time, datetime):
                posted_at = (
                    create_time.astimezone(UTC)
                    if create_time.tzinfo
                    else create_time.replace(tzinfo=UTC)
                )
            else:
                try:
                    posted_at = datetime.fromtimestamp(float(create_time), tz=UTC)
                except (TypeError, ValueError, OSError):
                    pass

        url = f"https://www.tiktok.com/@{handle}/video/{vid_id}"

        # TikTok はリツイートがないため share を代替
        return CollectedPost(
            post_id=f"tiktok_{vid_id}",
            author_handle=handle,
            author_name=name,
            text=desc,
            lang="und",
            url=url,
            source=f"tiktok#{source_tag}",
            retweet_count=share_count,
            like_count=like_count,
            reply_count=comment_count,
            posted_at=posted_at,
            raw_data={
                "play_count": play_count,
                "share_count": share_count,
                "comment_count": comment_count,
                "source_tag": source_tag,
            },
        )
    except Exception as exc:
        log.warning("tiktok_video_parse_failed", error=str(exc))
        return None
