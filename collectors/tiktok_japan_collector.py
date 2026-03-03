"""TikTok 日本トレンド特化コレクター.

日本語ロケール (ja-JP / Asia/Tokyo) でブラウザセッションを確立し、
region="JP" に上書きすることで日本向けコンテンツを優先して収集する。

収集対象:
    - 日本語ハッシュタグのトレンド動画
    - ショートドラマ専用ハッシュタグ (#ショートドラマ, #韓国ドラマ 等)
    - グローバルトレンド (region=JP フィルタ付き)

必要パッケージ:
    pip install TikTokApi playwright
    playwright install chromium

認証:
    TIKTOK_MS_TOKEN (Optional) — ブラウザ Cookie の msToken 値
    認証なしでも動作するが、ms_token があるとレート制限が緩い。
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime
from typing import Any

from Iran_ocint.collectors.base import AbstractCollector, CollectedPost
from Iran_ocint.utils.logger import get_logger
from Iran_ocint.utils.rate_limiter import tiktok_limiter

log = get_logger(__name__)

_tiktok_limiter = tiktok_limiter

_MIN_DELAY_SEC = 2.0
_MAX_DELAY_SEC = 5.0

def _has_display() -> bool:
    """実行時点でディスプレイが利用可能か判定する.

    モジュールインポート時ではなく呼び出し時に評価することで、
    _ensure_display() で DISPLAY を設定した後でも正しく動作する。

    Returns:
        DISPLAY または WAYLAND_DISPLAY が設定されている場合 True.
    """
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

# 日本向け Playwright コンテキスト設定
_JP_CONTEXT_OPTIONS: dict[str, Any] = {
    "locale": "ja-JP",
    "timezone_id": "Asia/Tokyo",
    "geolocation": {"longitude": 139.6917, "latitude": 35.6895},  # 東京
    "permissions": ["geolocation"],
}

# 日本語ハッシュタグ — 一般トレンド
JP_GENERAL_TAGS = [
    "日本",
    "おすすめ",
    "バズり",
    "トレンド",
    "fyp",
    "fypシ",
    "おすすめに乗りたい",
]

# ショートドラマ専用ハッシュタグ
JP_SHORT_DRAMA_TAGS = [
    "ショートドラマ",
    "韓国ドラマ",
    "恋愛ドラマ",
    "胸キュン",
    "泣けるドラマ",
    "縦型ドラマ",
    "スマドラ",
    "shortdrama",
    "lovestory",
    "恋愛",
]

# エンタメ系タグ
JP_ENTERTAINMENT_TAGS = [
    "ドラマ",
    "アニメ",
    "漫画",
    "音楽",
    "ダンス",
    "コメディ",
    "感動",
    "泣ける",
]


class TikTokJapanCollector(AbstractCollector):
    """日本トレンド特化 TikTok コレクター.

    日本語ロケール設定 + region=JP 上書きで日本向けコンテンツを優先収集する。
    ショートドラマ専用の収集メソッド `collect_short_dramas()` も提供する。

    Attributes:
        _api: TikTokApi インスタンス.
        _initialized: 初期化済みフラグ.
        _ms_token: TikTok ms_token (Optional).
        _mode: 収集モード ("general" | "drama" | "all").
    """

    def __init__(self, mode: str = "all") -> None:
        """コンストラクタ.

        Args:
            mode: 収集モード。
                "general" = 一般トレンド,
                "drama" = ショートドラマのみ,
                "all" = 両方.
        """
        self._api: Any = None
        self._initialized: bool = False
        self._ms_token: str = os.getenv("TIKTOK_MS_TOKEN", "")
        self._mode: str = mode

    async def initialize(self) -> None:
        """TikTokApi を日本語ロケールで初期化する.

        日本向け context_options (locale=ja-JP, timezone=Asia/Tokyo,
        geolocation=東京) を設定してセッションを確立する。
        セッション確立後に region パラメータを "JP" に上書きする。

        Raises:
            ImportError: TikTokApi がインストールされていない場合.
        """
        try:
            from TikTokApi import TikTokApi
        except ImportError:
            raise ImportError(
                "TikTokApi is required: pip install TikTokApi && playwright install chromium"
            )

        try:
            self._api = TikTokApi()
            await self._api.__aenter__()

            use_headless = not _has_display()
            # context_options (locale/timezone) は TikTok の networkidle を
            # ブロックしてタイムアウトを起こすため使用しない。
            # 代わりに session.params を後から region=JP に上書きする。
            session_kwargs: dict[str, Any] = {
                "num_sessions": 1,
                "headless": use_headless,
                "sleep_after": 3,
            }
            if self._ms_token:
                session_kwargs["ms_tokens"] = [self._ms_token]

            log.info(
                "tiktok_jp_creating_session",
                headless=use_headless,
                locale="ja-JP",
                region_target="JP",
            )
            await self._api.create_sessions(**session_kwargs)

            # セッションの ms_token を自動取得
            if self._api.sessions and not self._ms_token:
                extracted = getattr(self._api.sessions[0], "ms_token", None)
                if extracted:
                    self._ms_token = extracted
                    log.info("tiktok_jp_ms_token_extracted", length=len(extracted))

            # region パラメータを JP に上書き (API が US 固定のため手動修正)
            for session in self._api.sessions:
                if hasattr(session, "params") and session.params:
                    session.params["region"] = "JP"
                    session.params["app_language"] = "ja-JP"
                    session.params["browser_language"] = "ja-JP"
                    session.params["language"] = "ja"
                    session.params["tz_name"] = "Asia/Tokyo"
                    log.info("tiktok_jp_region_overridden", region="JP")

            self._initialized = True
            log.info("tiktok_jp_collector_initialized", mode=self._mode)

        except Exception as exc:
            log.error("tiktok_jp_init_failed", error=str(exc))
            self._initialized = False

    async def collect(
        self, queries: list[str], max_results: int = 30
    ) -> list[CollectedPost]:
        """指定ハッシュタグで日本向けコンテンツを収集する.

        Args:
            queries: ハッシュタグ名リスト (# 不要).
            max_results: クエリあたりの最大取得件数.

        Returns:
            CollectedPost のリスト.
        """
        if not self._initialized or self._api is None:
            log.warning("tiktok_jp_not_ready")
            return []

        all_posts: list[CollectedPost] = []
        for query in queries:
            try:
                posts = await self._collect_hashtag(query, max_results)
                all_posts.extend(posts)
                log.info("tiktok_jp_tag_done", tag=query, count=len(posts))
                await asyncio.sleep(random.uniform(_MIN_DELAY_SEC, _MAX_DELAY_SEC))
            except Exception as exc:
                log.error("tiktok_jp_tag_failed", tag=query, error=str(exc))

        return all_posts

    async def collect_jp_trending(self) -> list[CollectedPost]:
        """日本向けタグ一式でトレンドを収集する.

        モードに応じて収集対象タグを選択する。

        Returns:
            CollectedPost のリスト.
        """
        if self._mode == "drama":
            tags = JP_SHORT_DRAMA_TAGS
        elif self._mode == "general":
            tags = JP_GENERAL_TAGS
        else:
            tags = JP_GENERAL_TAGS + JP_SHORT_DRAMA_TAGS + JP_ENTERTAINMENT_TAGS

        return await self.collect(tags, max_results=20)

    async def collect_short_dramas(self, max_per_tag: int = 20) -> list[CollectedPost]:
        """ショートドラマ専用ハッシュタグで収集する.

        Args:
            max_per_tag: タグあたりの最大取得件数.

        Returns:
            CollectedPost のリスト (ドラマ系コンテンツのみ).
        """
        return await self.collect(JP_SHORT_DRAMA_TAGS, max_results=max_per_tag)

    async def get_trending_videos(self, count: int = 30) -> list[CollectedPost]:
        """グローバルトレンドから日本コンテンツを抽出する.

        グローバルトレンドを取得し、日本語テキストを含む動画でフィルタリングする。

        Args:
            count: 取得件数 (フィルタ前の件数).

        Returns:
            CollectedPost のリスト.
        """
        if not self._initialized or self._api is None:
            return []

        posts: list[CollectedPost] = []
        try:
            async with _tiktok_limiter:
                async for video in self._api.trending.videos(count=count):
                    post = _video_to_post(video, source_tag="trending_jp")
                    if post:
                        posts.append(post)

            # 日本語テキストを含む動画を優先 (先頭に移動)
            jp_posts = [p for p in posts if _is_japanese(p.text)]
            other_posts = [p for p in posts if not _is_japanese(p.text)]
            sorted_posts = jp_posts + other_posts

            log.info(
                "tiktok_jp_trending_collected",
                total=len(sorted_posts),
                japanese=len(jp_posts),
            )
        except Exception as exc:
            log.error("tiktok_jp_trending_failed", error=str(exc))
            sorted_posts = []

        return sorted_posts

    async def _collect_hashtag(
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
            async for video in hashtag.videos(count=min(max_count, 30)):
                post = _video_to_post(video, source_tag=clean_tag)
                if post:
                    posts.append(post)

        return posts

    async def shutdown(self) -> None:
        """TikTokApi セッションをクリーンアップする."""
        if self._api is not None:
            try:
                await self._api.__aexit__(None, None, None)
            except Exception as exc:
                log.warning("tiktok_jp_shutdown_error", error=str(exc))
            finally:
                self._api = None
        self._initialized = False
        log.info("tiktok_jp_collector_shutdown")


def _video_to_post(video: Any, source_tag: str) -> CollectedPost | None:
    """TikTokApi v7 の Video オブジェクトを CollectedPost に変換する.

    Args:
        video: TikTokApi の Video オブジェクト.
        source_tag: 収集元タグ名.

    Returns:
        CollectedPost インスタンス. 変換失敗時は None.
    """
    try:
        vid_id: str = str(video.id)
        author = video.author
        handle: str = getattr(author, "username", "") or "unknown"
        name: str = getattr(author, "nickname", handle) or handle

        raw = getattr(video, "as_dict", {}) or {}
        stats_raw = raw.get("stats") or getattr(video, "stats", None) or {}
        like_count: int = int(stats_raw.get("diggCount", 0) or 0)
        share_count: int = int(stats_raw.get("shareCount", 0) or 0)
        comment_count: int = int(stats_raw.get("commentCount", 0) or 0)
        play_count: int = int(stats_raw.get("playCount", 0) or 0)

        desc: str = raw.get("desc", "") or ""

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

        return CollectedPost(
            post_id=f"tiktok_{vid_id}",
            author_handle=handle,
            author_name=name,
            text=desc,
            lang="ja" if _is_japanese(desc) else "und",
            url=url,
            source=f"tiktok_jp#{source_tag}",
            retweet_count=share_count,
            like_count=like_count,
            reply_count=comment_count,
            posted_at=posted_at,
            raw_data={
                "play_count": play_count,
                "source_tag": source_tag,
                "is_japanese": _is_japanese(desc),
            },
        )
    except Exception as exc:
        log.warning("tiktok_jp_video_parse_failed", error=str(exc))
        return None


def _is_japanese(text: str) -> bool:
    """テキストに日本語 (ひらがな/カタカナ/漢字) が含まれるか判定する.

    Args:
        text: 判定対象テキスト.

    Returns:
        日本語文字が1文字以上含まれる場合 True.
    """
    if not text:
        return False
    for char in text:
        cp = ord(char)
        # ひらがな: U+3040-U+309F
        # カタカナ: U+30A0-U+30FF
        # CJK統合漢字: U+4E00-U+9FFF
        # 半角カタカナ: U+FF65-U+FF9F
        if (
            0x3040 <= cp <= 0x309F
            or 0x30A0 <= cp <= 0x30FF
            or 0x4E00 <= cp <= 0x9FFF
            or 0xFF65 <= cp <= 0xFF9F
        ):
            return True
    return False
