"""TikTok FYP (おすすめ) クローラー.

Playwright で TikTok トップページを実際にスクロールし、
表示された動画を DOM から取得する「本物のFYP閲覧」アプローチ。

タグ検索と違い、TikTok のレコメンドアルゴリズムが表示するコンテンツを
そのまま収集するため、トレンドの実態に近いデータが得られる。

必要パッケージ:
    pip install playwright
    playwright install chromium

動画 AI 説明:
    pip install yt-dlp google-genai
    環境変数 GEMINI_API_KEY を設定

注意:
    日本 IP がない場合でも region=JP パラメータと ja-JP ブラウザ設定で
    日本寄りのコンテンツが表示されやすくなるが、完全な日本向けFYPには
    日本 IP (VPN/プロキシ) が理想的。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# JST タイムゾーン (pytz 不要の固定オフセット)
_JST = timezone(timedelta(hours=9))

from utils.logger import get_logger

log = get_logger(__name__)

# ページをスクロールして動画を収集する際の待機時間 (秒)
_SCROLL_WAIT_SEC = 2.5
_SCROLL_AMOUNT_PX = 800

# Gemini モデル (動画理解対応モデル)
# models/ プレフィックスが必要。フォールバック順で試みる。
_GEMINI_MODEL = "models/gemini-2.5-flash"
_GEMINI_MODEL_FALLBACKS = [
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
    "models/gemini-2.0-flash-lite",
    "models/gemini-flash-latest",
]


@dataclass
class FYPVideo:
    """FYP から収集した動画情報.

    Attributes:
        video_id: TikTok 動画 ID.
        author_handle: 投稿者ハンドル.
        description: キャプションテキスト.
        url: 動画 URL.
        like_count: いいね数.
        comment_count: コメント数.
        share_count: シェア数.
        play_count: 再生数.
        duration_sec: 動画の長さ (秒).
        cover_url: サムネイル画像 URL.
        region_code: 投稿者の地域コード (例: "JP", "US"). API から取得。
        collected_at: 収集日時.
        ai_description: Gemini による AI 説明文.
        ai_tags: AI が推定したカテゴリタグ.
        raw_data: 生データ.
    """

    video_id: str
    author_handle: str
    description: str
    url: str
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    play_count: int = 0
    duration_sec: int = 0
    cover_url: str = ""
    region_code: str = ""                      # API の author.region (例: "JP")
    posted_at: datetime | None = None          # 動画の投稿日時 (UTC)
    top_comment: dict = field(default_factory=dict)  # 最多いいねコメント {text, author, likes}
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ai_description: str = ""
    ai_tags: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)

    def posted_at_jst(self) -> str:
        """投稿日時を JST 文字列で返す.

        Returns:
            "YYYY/MM/DD HH:MM" 形式 (JST)。不明の場合は空文字列。
        """
        if not self.posted_at:
            return ""
        jst = self.posted_at.astimezone(_JST)
        return jst.strftime("%Y/%m/%d %H:%M")

    def is_japanese(self) -> bool:
        """日本語コンテンツか判定する.

        判定優先順位:
        1. API の region_code == "JP" (投稿者が日本在住)
        2. ハッシュタグを除いたキャプションにひらがな/カタカナが含まれる

        ※ CJK漢字は中国語・韓国語でも共有されるため使用しない。
        ※ #fypシ のようなタグは世界中の非日本語クリエイターが使うため、
          ハッシュタグ内の文字は判定から除外する。

        Returns:
            日本語コンテンツと判定された場合 True.
        """
        # API のリージョン情報を最優先 (投稿者アカウントの登録国)
        if self.region_code == "JP":
            return True
        # ハッシュタグを除去してからひらがな/カタカナで判定
        # (#fypシ など非日本語クリエイターが使うタグの誤検知を防ぐ)
        text_without_tags = re.sub(r"#\S+", "", self.description)
        for char in text_without_tags:
            cp = ord(char)
            if 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:  # ひらがな or カタカナ
                return True
        return False


class TikTokFYPCrawler:
    """Playwright で TikTok FYP を実際にスクロールして収集するクローラー.

    タグ検索ではなく、アルゴリズムが表示するコンテンツをそのまま取得する。
    ブラウザの「ネットワーク」タブに相当するリクエストインターセプトと
    DOM スクレイピングの両方を使用する。

    Attributes:
        _browser: Playwright Browser インスタンス.
        _page: Playwright Page インスタンス.
        _collected_ids: 重複防止のための収集済み動画 ID セット.
    """

    def __init__(self) -> None:
        """コンストラクタ."""
        self._browser: Any = None
        self._page: Any = None
        self._playwright: Any = None
        self._collected_ids: set[str] = set()
        # APIレスポンスをインターセプトして収集するバッファ
        self._api_buffer: list[dict] = []

    async def initialize(self, headless: bool | None = None) -> None:
        """Playwright ブラウザを日本語設定で起動する.

        Args:
            headless: None の場合は DISPLAY 環境変数で自動判定.
        """
        from playwright.async_api import async_playwright

        if headless is None:
            headless = not bool(
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--lang=ja-JP",
            ],
        )

        # 日本語ロケール設定でコンテキスト作成
        context = await self._browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        self._page = await context.new_page()

        # TikTok APIレスポンスをインターセプト
        self._page.on("response", self._on_response)

        log.info("fyp_crawler_initialized", headless=headless)

    async def _on_response(self, response: Any) -> None:
        """TikTok の item_list APIレスポンスをインターセプトする.

        Args:
            response: Playwright Response オブジェクト.
        """
        if "item_list" in response.url or "recommend" in response.url:
            try:
                data = await response.json()
                items = data.get("itemList") or data.get("data", {}).get("items", [])
                if items:
                    self._api_buffer.extend(items)
                    log.info(
                        "fyp_api_intercepted",
                        url=response.url[:80],
                        items=len(items),
                    )
            except Exception:
                pass

    async def crawl_fyp(
        self,
        scroll_count: int = 10,
        language: str = "ja",
        wait_between: float = _SCROLL_WAIT_SEC,
    ) -> list[FYPVideo]:
        """TikTok のおすすめフィードをスクロールして動画を収集する.

        実際のブラウザ操作 (スクロール) で FYP に表示される動画を収集する。
        DOM からの取得 + APIレスポンスインターセプトの2経路で収集する。

        Args:
            scroll_count: スクロール回数 (1回で3〜5件取得が目安).
            language: 優先言語 ("ja" = 日本語フィルタ優先).
            wait_between: スクロール間の待機秒数.

        Returns:
            FYPVideo のリスト.
        """
        if self._page is None:
            raise RuntimeError("initialize() を先に呼んでください")

        log.info("fyp_crawl_started", scroll_count=scroll_count)

        # TikTok トップページに移動 (lang=ja-JP で日本向けコンテンツを優先)
        await self._page.goto(
            "https://www.tiktok.com/?lang=ja-JP",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

        # 「後で」ポップアップを閉じる
        try:
            await self._page.click(
                'button:has-text("後で"), button:has-text("Later"), [data-e2e="close-icon"]',
                timeout=3000,
            )
        except Exception:
            pass

        # 全スクロール分の DOM 動画を蓄積するリスト
        all_dom_videos: list[FYPVideo] = []

        # スクロールして動画を収集
        for i in range(scroll_count):
            await asyncio.sleep(wait_between)

            # DOM から動画リンクを収集 (各スクロールで新規分のみ返る)
            new_dom = await self._extract_from_dom()
            all_dom_videos.extend(new_dom)
            log.info(
                "fyp_scroll",
                scroll=i + 1,
                dom_new=len(new_dom),
                dom_total=len(all_dom_videos),
                api_buffered=len(self._api_buffer),
            )

            # ページをスクロール
            await self._page.evaluate(f"window.scrollBy(0, {_SCROLL_AMOUNT_PX})")

        # APIバッファからも動画を生成 (DOM で取得できなかった統計情報を補完)
        api_videos = self._parse_api_buffer()

        # API データを優先して DOM データとマージ (API の方がスタッツが正確)
        api_map = {v.video_id: v for v in api_videos}
        merged: list[FYPVideo] = []
        seen: set[str] = set()
        for v in all_dom_videos + api_videos:
            if v.video_id in seen:
                continue
            seen.add(v.video_id)
            # DOM 動画は API データで上書き (スタッツが正確)
            merged.append(api_map.get(v.video_id, v))

        all_videos = merged

        jp_count = sum(1 for v in all_videos if v.is_japanese())

        if language == "ja":
            # 日本語コンテンツのみに絞り込む
            all_videos = [v for v in all_videos if v.is_japanese()]
        else:
            # "all": 日本語を先頭に、それ以外を後ろに並べる
            jp = [v for v in all_videos if v.is_japanese()]
            other = [v for v in all_videos if not v.is_japanese()]
            all_videos = jp + other

        log.info(
            "fyp_crawl_completed",
            total=len(all_videos),
            japanese=jp_count,
            language_filter=language,
        )
        return all_videos

    async def _extract_from_dom(self) -> list[FYPVideo]:
        """DOM から現在表示中の動画情報を取得する.

        Returns:
            FYPVideo のリスト.
        """
        try:
            # TikTok の動画カードセレクタ
            video_data = await self._page.evaluate("""
                () => {
                    const videos = [];
                    // 動画カードを探す (TikTok のクラス名は難読化されているため属性で探す)
                    const links = document.querySelectorAll('a[href*="/video/"]');
                    links.forEach(link => {
                        const href = link.href || '';
                        const match = href.match(/@([^/]+)[/]video[/]([0-9]+)/);
                        if (!match) return;
                        
                        const [, handle, videoId] = match;
                        
                        // テキストを周辺DOMから取得
                        const card = link.closest('[class*="DivItemContainer"], [class*="video-card"], article') 
                                  || link.parentElement;
                        const desc = card ? (card.textContent || '').substring(0, 200).trim() : '';
                        
                        // 数値 (likes/comments) を取得
                        const nums = card ? [...card.querySelectorAll('[data-e2e*="like-count"], [data-e2e*="comment-count"]')] : [];
                        const counts = nums.map(el => {
                            const t = el.textContent || '0';
                            // "1.2M" "324.5K" などをパース
                            const n = parseFloat(t);
                            if (t.includes('M')) return Math.round(n * 1000000);
                            if (t.includes('K')) return Math.round(n * 1000);
                            return Math.round(n) || 0;
                        });
                        
                        videos.push({
                            videoId,
                            handle,
                            desc,
                            url: href,
                            likeCount: counts[0] || 0,
                            commentCount: counts[1] || 0,
                        });
                    });
                    return videos;
                }
            """)

            results: list[FYPVideo] = []
            for item in (video_data or []):
                vid_id = item.get("videoId", "")
                if not vid_id or vid_id in self._collected_ids:
                    continue
                self._collected_ids.add(vid_id)

                results.append(FYPVideo(
                    video_id=vid_id,
                    author_handle=item.get("handle", ""),
                    description=item.get("desc", ""),
                    url=item.get("url", ""),
                    like_count=item.get("likeCount", 0),
                    comment_count=item.get("commentCount", 0),
                    raw_data={"source": "dom"},
                ))

            return results

        except Exception as exc:
            log.warning("fyp_dom_extract_failed", error=str(exc))
            return []

    def _parse_api_buffer(self) -> list[FYPVideo]:
        """インターセプトした API レスポンスバッファをパースする.

        DOM 取得済みのものも含めて全件返す。
        呼び出し側でマージ・重複排除を行うため、ここでは _collected_ids を参照しない。

        Returns:
            FYPVideo のリスト.
        """
        results: list[FYPVideo] = []
        seen_in_buffer: set[str] = set()
        for item in self._api_buffer:
            try:
                vid_id = str(item.get("id", ""))
                if not vid_id or vid_id in seen_in_buffer:
                    continue
                seen_in_buffer.add(vid_id)

                author = item.get("author", {}) or {}
                stats = item.get("stats", {}) or {}
                video_meta = item.get("video", {}) or {}
                desc = item.get("desc", "") or ""
                handle = author.get("uniqueId", "") or author.get("nickname", "")

                # 地域コード: author.region が最も信頼性が高い
                region_code = str(
                    author.get("region", "")
                    or item.get("regionCode", "")
                    or ""
                ).upper()

                # サムネイル: originCover → dynamicCover → cover の順で優先
                cover = (
                    video_meta.get("originCover")
                    or video_meta.get("dynamicCover")
                    or video_meta.get("cover")
                    or ""
                )

                # 投稿日時 (Unix 秒 → UTC datetime)
                create_ts = int(item.get("createTime", 0) or 0)
                posted_at = datetime.fromtimestamp(create_ts, tz=UTC) if create_ts else None

                results.append(FYPVideo(
                    video_id=vid_id,
                    author_handle=handle,
                    description=desc,
                    url=f"https://www.tiktok.com/@{handle}/video/{vid_id}",
                    like_count=int(stats.get("diggCount", 0) or 0),
                    comment_count=int(stats.get("commentCount", 0) or 0),
                    share_count=int(stats.get("shareCount", 0) or 0),
                    play_count=int(stats.get("playCount", 0) or 0),
                    duration_sec=int(video_meta.get("duration", 0) or 0),
                    cover_url=cover,
                    region_code=region_code,
                    posted_at=posted_at,
                    raw_data={"source": "api_intercept"},
                ))
            except Exception as exc:
                log.warning("fyp_api_parse_failed", error=str(exc))
                continue

        return results

    async def fetch_top_comment(self, video_id: str) -> dict:
        """指定動画の最多いいねコメントを TikTok コメント API から取得する.

        ブラウザのセッション（Cookie）を使って認証済みリクエストを行う。
        取得した N 件のコメントから digg_count (いいね数) が最大のものを返す。

        Args:
            video_id: TikTok 動画 ID.

        Returns:
            {"text": str, "author": str, "likes": int} の辞書。
            取得失敗時は空辞書。
        """
        if self._page is None:
            return {}

        api_url = (
            f"https://www.tiktok.com/api/comment/list/"
            f"?aweme_id={video_id}&count=20&cursor=0&aid=1988"
        )
        try:
            # Playwright の fetch でブラウザ Cookie を引き継いでリクエスト
            response = await self._page.request.get(api_url, timeout=10000)
            if not response.ok:
                log.warning("comment_api_error", status=response.status, video_id=video_id)
                return {}

            data = await response.json()
            comments = data.get("comments") or []
            if not comments:
                return {}

            # digg_count (いいね数) が最多のコメントを選ぶ
            top = max(comments, key=lambda c: int(c.get("digg_count", 0) or 0))
            text = top.get("text", "")
            author = top.get("user", {}).get("unique_id", "") or top.get("user", {}).get("nickname", "")
            likes = int(top.get("digg_count", 0) or 0)

            log.info("top_comment_fetched", video_id=video_id, likes=likes)
            return {"text": text, "author": author, "likes": likes}

        except Exception as exc:
            log.warning("comment_fetch_failed", video_id=video_id, error=str(exc)[:80])
            return {}

    async def fetch_top_comments(
        self,
        video_ids: list[str],
        delay_sec: float = 1.5,
    ) -> dict[str, dict]:
        """複数動画の最多いいねコメントをまとめて取得する.

        レート制限対策として各リクエスト間に待機を挟む。

        Args:
            video_ids: TikTok 動画 ID のリスト.
            delay_sec: リクエスト間の待機秒数.

        Returns:
            {video_id: コメント辞書} のマッピング。
        """
        results: dict[str, dict] = {}
        for i, vid_id in enumerate(video_ids):
            comment = await self.fetch_top_comment(vid_id)
            if comment:
                results[vid_id] = comment
            if i < len(video_ids) - 1:
                await asyncio.sleep(delay_sec)
        return results

    async def shutdown(self) -> None:
        """Playwright をクリーンアップする."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            log.warning("fyp_crawler_shutdown_error", error=str(exc))
        log.info("fyp_crawler_shutdown")


class VideoAnalyzer:
    """Gemini API を使って TikTok 動画を説明するアナライザー.

    yt-dlp で動画をダウンロードし、Gemini の動画理解機能で
    内容・カテゴリ・トレンド要因を分析する。

    Attributes:
        _api_key: Gemini API キー.
        _model: 使用するモデル名.
        _download_dir: 動画一時保存ディレクトリ.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _GEMINI_MODEL,
        download_dir: str | None = None,
    ) -> None:
        """コンストラクタ.

        Args:
            api_key: Gemini API キー。None の場合は GEMINI_API_KEY 環境変数を使用.
            model: Gemini モデル名.
            download_dir: 動画保存先ディレクトリ。None の場合は tempdir を使用.
        """
        self._api_key: str = api_key or os.getenv("GEMINI_API_KEY", "")
        self._model: str = model
        self._download_dir: Path = Path(download_dir) if download_dir else Path(tempfile.mkdtemp(prefix="tiktok_"))

    def is_configured(self) -> bool:
        """Gemini API キーが設定されているか確認する.

        Returns:
            API キーが設定されている場合 True.
        """
        return bool(self._api_key)

    async def analyze_video(
        self, url: str, extra_context: str = "", include_translation: bool = False
    ) -> dict[str, Any]:
        """TikTok 動画をダウンロードして Gemini で分析する.

        Args:
            url: TikTok 動画 URL.
            extra_context: 既知の説明文・ハッシュタグなど追加コンテキスト.
            include_translation: True の場合、キャプション・音声の日本語訳も生成する.

        Returns:
            分析結果辞書 (description, category, trend_reason, tags, language, translation?).
        """
        if not self.is_configured():
            return {"error": "GEMINI_API_KEY が設定されていません"}

        video_path = await self._download_video(url)
        if not video_path:
            return {"error": "動画ダウンロード失敗"}

        try:
            result = await self._call_gemini(video_path, url, extra_context, include_translation)
            return result
        finally:
            # 一時ファイル削除
            try:
                video_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _download_video(self, url: str) -> Path | None:
        """yt-dlp で TikTok 動画をダウンロードする.

        最大30秒・720p以下で取得し、ファイルサイズを抑える。

        Args:
            url: TikTok 動画 URL.

        Returns:
            ダウンロードしたファイルパス。失敗時は None.
        """
        import subprocess

        out_path = self._download_dir / f"tiktok_{_extract_video_id(url)}.mp4"

        cmd = [
            "yt-dlp",
            "--quiet",
            "--no-warnings",
            "--format", "mp4[height<=720]/best[ext=mp4]",
            "--output", str(out_path),
            "--max-filesize", "50m",
            "--no-playlist",
            url,
        ]

        try:
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, timeout=60),
            )
            if proc.returncode == 0 and out_path.exists():
                size_mb = out_path.stat().st_size / 1024 / 1024
                log.info("video_downloaded", url=url[-40:], size_mb=f"{size_mb:.1f}")
                return out_path
            else:
                log.warning("video_download_failed", stderr=proc.stderr.decode()[:200])
                return None
        except Exception as exc:
            log.error("video_download_error", error=str(exc))
            return None

    async def _call_gemini(
        self, video_path: Path, url: str, extra_context: str,
        include_translation: bool = False,
    ) -> dict[str, Any]:
        """Gemini API に動画を送信して分析する.

        レート制限 (429) に当たった場合は別モデルにフォールバックする。

        Args:
            video_path: 動画ファイルパス.
            url: 元の TikTok URL.
            extra_context: ハッシュタグ・キャプションなどの追加コンテキスト.
            include_translation: True の場合、キャプション・音声の日本語訳も生成する.

        Returns:
            分析結果辞書.
        """
        try:
            from google import genai
            from google.genai import types
            from google.genai.errors import ClientError
        except ImportError:
            return {"error": "google-genai がインストールされていません: pip install google-genai"}

        client = genai.Client(api_key=self._api_key)

        # 翻訳フィールドは非日本語コンテンツの場合のみ要求
        translation_field = (
            '  "translation": "キャプション・音声・テキストの日本語訳 (元が日本語の場合は null)",'
            if include_translation else ""
        )

        prompt = f"""この TikTok 動画を日本語で分析してください。

元のキャプション/ハッシュタグ: {extra_context or '(なし)'}
URL: {url}

以下の項目を JSON 形式で返してください:
{{
  "description": "動画の内容を3〜5文で説明 (日本語)",
  "category": "カテゴリ (例: ショートドラマ, ダンス, ペット, 料理, コメディ, ニュース, etc.)",
  "trend_reason": "なぜバズっているか・トレンドになっている理由の推測",
  "tags": ["提案するハッシュタグ (日本語) を5個まで"],
  "language": "動画内で主に使われている言語",
  "emotion": "視聴者が感じる主な感情 (例: 感動, 笑い, 驚き, 共感)",{translation_field}
}}"""

        loop = asyncio.get_event_loop()

        # モデルのフォールバック順序: 設定モデル → 他のモデル
        models_to_try = [self._model] + [
            m for m in _GEMINI_MODEL_FALLBACKS if m != self._model
        ]

        for model_name in models_to_try:
            log.info("gemini_trying_model", model=model_name)

            def _upload_and_call(model: str = model_name) -> str:
                """同期的にファイルアップロードと API 呼び出しを行う.

                ファイルが ACTIVE 状態になるまでポーリングしてから
                generate_content を呼び出す。
                """
                import time

                with open(video_path, "rb") as f:
                    uploaded = client.files.upload(
                        file=f,
                        config=types.UploadFileConfig(
                            mime_type="video/mp4",
                            display_name=video_path.name,
                        ),
                    )

                # ACTIVE になるまで最大30秒ポーリング
                max_wait = 30
                poll_interval = 2
                elapsed = 0
                file_obj = uploaded
                while getattr(file_obj, "state", None) not in (None, "ACTIVE") and elapsed < max_wait:
                    time.sleep(poll_interval)
                    elapsed += poll_interval
                    try:
                        file_obj = client.files.get(name=uploaded.name)
                    except Exception:
                        break

                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=[
                            types.Part.from_uri(
                                file_uri=uploaded.uri,
                                mime_type="video/mp4",
                            ),
                            prompt,
                        ],
                    )
                    return response.text or ""
                finally:
                    try:
                        client.files.delete(name=uploaded.name)
                    except Exception:
                        pass

            try:
                raw_text = await loop.run_in_executor(None, _upload_and_call)

                # JSON 部分を抽出
                try:
                    json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
                        result["_model_used"] = model_name
                        return result
                except (json.JSONDecodeError, AttributeError):
                    pass

                return {"raw_response": raw_text, "_model_used": model_name}

            except ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    _idx = models_to_try.index(model_name)
                    _next = models_to_try[_idx + 1] if _idx + 1 < len(models_to_try) else "none"
                    log.warning("gemini_rate_limit", model=model_name, fallback=_next)
                    await asyncio.sleep(2)
                    continue
                return {"error": str(e), "_model_used": model_name}
            except Exception as e:
                return {"error": str(e), "_model_used": model_name}

        return {"error": "全モデルでレート制限に達しました。しばらく待ってから再実行してください。"}


def _extract_video_id(url: str) -> str:
    """URL から TikTok 動画 ID を抽出する.

    Args:
        url: TikTok 動画 URL.

    Returns:
        動画 ID 文字列。抽出失敗時は "unknown".
    """
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else "unknown"
