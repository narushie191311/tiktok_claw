"""Instagram トレンドコレクター.

instagrapi (非公式 Instagram プライベート API) を使い、
ハッシュタグのトップ投稿・最新投稿を収集する。

必要パッケージ:
    pip install instagrapi

認証:
    環境変数 INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD を設定するか、
    INSTAGRAM_SESSION_FILE に既存セッション JSON のパスを指定する。
    2FA 対応: INSTAGRAM_TOTP_SECRET で TOTP シークレットを設定。

注意:
    Instagram の利用規約上、スクレイピングは禁止されている。
    レート制限・アカウント一時凍結のリスクを理解した上で使用すること。
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collectors.base import AbstractCollector, CollectedPost
from utils.logger import get_logger
from utils.rate_limiter import instagram_limiter

log = get_logger(__name__)

_instagram_limiter = instagram_limiter

# Instagram リクエスト間の遅延 (秒)
_MIN_DELAY_SEC = 3.0
_MAX_DELAY_SEC = 8.0

# セッションファイルデフォルトパス
_DEFAULT_SESSION_FILE = "Iran_ocint/data/instagram_session.json"


class InstagramCollector(AbstractCollector):
    """instagrapi を使ったハッシュタグ収集コレクター.

    ハッシュタグのトップ/最新投稿を取得する。
    セッション再利用で不要ログインを回避する。

    Attributes:
        _client: instagrapi の Client インスタンス.
        _initialized: 初期化済みフラグ.
        _username: Instagram ユーザー名.
        _password: Instagram パスワード.
        _session_file: セッション保存ファイルパス.
        _totp_secret: 2FA TOTP シークレット (Optional).
    """

    def __init__(self) -> None:
        """コンストラクタ."""
        self._client: Any = None
        self._initialized: bool = False
        self._username: str = os.getenv("INSTAGRAM_USERNAME", "")
        self._password: str = os.getenv("INSTAGRAM_PASSWORD", "")
        self._session_file: str = os.getenv(
            "INSTAGRAM_SESSION_FILE", _DEFAULT_SESSION_FILE
        )
        self._totp_secret: str = os.getenv("INSTAGRAM_TOTP_SECRET", "")

    async def initialize(self) -> None:
        """instagrapi クライアントを初期化し認証する.

        既存セッションファイルがあれば再ロードする。
        なければ username/password でログインしてセッションを保存。

        Raises:
            ImportError: instagrapi がインストールされていない場合.
            ConnectionError: 認証情報が不足している場合.
        """
        try:
            from instagrapi import Client
            from instagrapi.exceptions import LoginRequired, TwoFactorRequired
        except ImportError:
            log.error(
                "instagrapi_not_installed",
                hint="pip install instagrapi",
            )
            raise ImportError("instagrapi is required. Install with: pip install instagrapi")

        if not self._username:
            log.warning(
                "instagram_credentials_missing",
                hint="Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in .env",
            )
            return

        # asyncio スレッドオフロード (instagrapi は同期 API)
        self._client = Client()
        self._client.delay_range = [1, 3]  # リクエスト間のランダム遅延 (秒)

        session_path = Path(self._session_file)

        if session_path.exists():
            try:
                self._client.load_settings(session_path)
                self._client.login(self._username, self._password)
                log.info("instagram_session_loaded", file=str(session_path))
                self._initialized = True
                return
            except (LoginRequired, Exception) as exc:
                log.warning(
                    "instagram_session_expired",
                    error=str(exc),
                    fallback="re-login",
                )

        # 新規ログイン
        try:
            login_kwargs: dict[str, Any] = {}
            if self._totp_secret:
                login_kwargs["verification_code"] = _get_totp_code(self._totp_secret)

            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.login(
                    self._username, self._password, **login_kwargs
                ),
            )

            # セッション保存
            session_path.parent.mkdir(parents=True, exist_ok=True)
            self._client.dump_settings(session_path)

            self._initialized = True
            log.info("instagram_login_success", username=self._username)

        except Exception as exc:
            log.error("instagram_login_failed", error=str(exc))
            self._initialized = False

    async def collect(
        self, queries: list[str], max_results: int = 50
    ) -> list[CollectedPost]:
        """Instagram ハッシュタグ検索で投稿を収集する.

        Args:
            queries: ハッシュタグ名 (# 不要) のリスト.
            max_results: クエリあたりの最大取得件数.

        Returns:
            CollectedPost のリスト. 未初期化時は空リスト.
        """
        if not self._initialized or self._client is None:
            log.warning("instagram_not_ready", msg="Skipping — not initialized")
            return []

        all_posts: list[CollectedPost] = []

        for query in queries:
            try:
                posts = await self._collect_hashtag(query, max_results)
                all_posts.extend(posts)
                log.info("instagram_query_done", query=query, count=len(posts))

                await asyncio.sleep(random.uniform(_MIN_DELAY_SEC, _MAX_DELAY_SEC))

            except Exception as exc:
                log.error("instagram_query_failed", query=query, error=str(exc))
                continue

        return all_posts

    async def _collect_hashtag(
        self, tag: str, max_count: int
    ) -> list[CollectedPost]:
        """ハッシュタグのトップ/最新投稿を取得する.

        instagrapi は同期ライブラリのため executor でオフロードする。

        Args:
            tag: ハッシュタグ名 (# 有無を自動除去).
            max_count: 最大取得件数.

        Returns:
            CollectedPost のリスト.
        """
        clean_tag = tag.lstrip("#").strip()
        loop = asyncio.get_event_loop()

        def _fetch() -> list[Any]:
            """同期フェッチ関数 (executor 内で実行)."""
            try:
                # top 投稿 + recent 投稿を合わせて取得
                medias = self._client.hashtag_medias_top(clean_tag, amount=min(max_count // 2, 9))
                recent = self._client.hashtag_medias_recent_v1(clean_tag, max_amount=max_count // 2)
                return list(medias) + list(recent)
            except Exception as exc:
                log.warning("instagram_hashtag_fetch_failed", tag=clean_tag, error=str(exc))
                return []

        raw_medias = await loop.run_in_executor(None, _fetch)

        posts: list[CollectedPost] = []
        for media in raw_medias:
            post = _media_to_post(media, source_tag=clean_tag)
            if post:
                posts.append(post)

        # 重複 ID 除去
        seen: set[str] = set()
        unique: list[CollectedPost] = []
        for p in posts:
            if p.post_id not in seen:
                seen.add(p.post_id)
                unique.append(p)

        return unique

    async def shutdown(self) -> None:
        """instagrapi クライアントをクリーンアップする."""
        if self._client is not None:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._client.logout
                )
            except Exception as exc:
                log.warning("instagram_logout_error", error=str(exc))
            finally:
                self._client = None
        self._initialized = False
        log.info("instagram_collector_shutdown")


def _media_to_post(media: Any, source_tag: str) -> CollectedPost | None:
    """instagrapi の Media オブジェクトを CollectedPost に変換する.

    Args:
        media: instagrapi の Media オブジェクト.
        source_tag: 収集元タグ名.

    Returns:
        CollectedPost インスタンス. 変換失敗時は None.
    """
    try:
        media_id: str = str(media.id)
        user = media.user

        handle: str = getattr(user, "username", "") or "unknown"
        name: str = getattr(user, "full_name", handle) or handle

        caption_obj = getattr(media, "caption_text", None)
        text: str = str(caption_obj) if caption_obj else ""

        like_count: int = int(getattr(media, "like_count", 0) or 0)
        comment_count: int = int(getattr(media, "comment_count", 0) or 0)

        taken_at = getattr(media, "taken_at", None)
        posted_at: datetime | None = None
        if taken_at:
            if isinstance(taken_at, datetime):
                posted_at = taken_at.astimezone(UTC)
            else:
                try:
                    posted_at = datetime.fromtimestamp(float(taken_at), tz=UTC)
                except (TypeError, ValueError):
                    pass

        # shortcode から URL 生成
        shortcode: str = getattr(media, "code", media_id)
        url = f"https://www.instagram.com/p/{shortcode}/"

        return CollectedPost(
            post_id=f"ig_{media_id}",
            author_handle=handle,
            author_name=name,
            text=text,
            lang="und",
            url=url,
            source=f"instagram#{source_tag}",
            retweet_count=0,  # Instagram にシェアカウントなし
            like_count=like_count,
            reply_count=comment_count,
            posted_at=posted_at,
            raw_data={
                "media_type": str(getattr(media, "media_type", "")),
                "source_tag": source_tag,
            },
        )
    except Exception as exc:
        log.warning("instagram_media_parse_failed", error=str(exc))
        return None


def _get_totp_code(secret: str) -> str:
    """TOTP シークレットから現在の認証コードを生成する.

    Args:
        secret: Base32 エンコードされた TOTP シークレット.

    Returns:
        6桁の TOTP コード文字列.

    Raises:
        ImportError: pyotp がインストールされていない場合.
    """
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        log.error("pyotp_not_installed", hint="pip install pyotp")
        raise ImportError("pyotp is required for 2FA. Install with: pip install pyotp")
