"""Slack Webhook 通知ユーティリティ.

TikTok FYP トレンド動画の収集結果と Gemini AI 分析結果を
Slack の Block Kit 形式で整形して Incoming Webhook に送信する。

使用方法:
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/...")
    await notifier.send_trend_report(videos, ai_results)

Slack アプリの設定:
    1. https://api.slack.com/apps → "Create New App" → "From scratch"
    2. "Incoming Webhooks" を有効化 → "Add New Webhook to Workspace"
    3. チャンネルを選択 → Webhook URL をコピー
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from Iran_ocint.utils.logger import get_logger

if TYPE_CHECKING:
    from Iran_ocint.collectors.tiktok_fyp_crawler import FYPVideo

log = get_logger(__name__)

# Slack Block Kit の最大テキスト長
_SLACK_TEXT_MAX = 3000
# 1回の Webhook 送信あたりの最大ブロック数 (Slack 制限: 50)
_MAX_BLOCKS_PER_MESSAGE = 48


def _fmt_count(n: int) -> str:
    """数値を読みやすい形式にフォーマットする.

    Args:
        n: 数値.

    Returns:
        フォーマット済み文字列 (例: "220万", "93.1K", "5,234").
    """
    if n >= 100_000_000:
        return f"{n/100_000_000:.1f}億"
    if n >= 10_000:
        return f"{n/10_000:.1f}万"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


def _fmt_duration(sec: int) -> str:
    """秒数を mm:ss 形式に変換する.

    Args:
        sec: 秒数.

    Returns:
        "1:23" 形式の文字列、0秒の場合は空文字列.
    """
    if not sec:
        return ""
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"


def _build_video_block(
    video: "FYPVideo",
    rank: int,
    ai_result: dict[str, Any] | None = None,
) -> list[dict]:
    """1件の動画情報を Slack Block Kit ブロックのリストに変換する.

    Args:
        video: FYPVideo インスタンス.
        rank: ランキング順位 (1始まり).
        ai_result: Gemini 分析結果辞書 (None の場合は AI セクション省略).

    Returns:
        Slack Block Kit ブロックのリスト.
    """
    duration_str = _fmt_duration(video.duration_sec)
    # 説明文からハッシュタグを除去（タグ行に集約）
    raw_desc = video.description or "(テキストなし)"
    desc = re.sub(r"#\S+", "", raw_desc).strip()[:120].replace("\n", " ")
    # 実際のハッシュタグを抽出
    real_tags = re.findall(r"#(\S+)", video.description or "")

    # ヘッダー行: 順位 + 投稿者 + 投稿日時
    rank_str = f"#{rank}"
    posted = video.posted_at_jst() if hasattr(video, "posted_at_jst") else ""
    posted_str = f"  📅 {posted}" if posted else ""
    header_text = f"*{rank_str}  <{video.url}|@{video.author_handle}>*{posted_str}"

    # 統計行
    stats_parts = [
        f"❤️ *{_fmt_count(video.like_count)}*",
        f"💬 {_fmt_count(video.comment_count)}",
        f"🔁 {_fmt_count(video.share_count)}",
        f"▶️  {_fmt_count(video.play_count)}",
    ]
    if duration_str:
        stats_parts.append(f"⏱ {duration_str}")
    stats_text = "  ".join(stats_parts)

    # キャプション
    main_text = f"{header_text}\n{stats_text}\n_{desc}_"

    # 最多いいねコメント: 「text」（❤️N万）
    top_comment = getattr(video, "top_comment", {}) or {}
    if top_comment.get("text"):
        c_text = top_comment["text"][:80].replace("\n", " ")
        c_likes = _fmt_count(top_comment.get("likes", 0))
        main_text += f"\n「{c_text}」（❤️{c_likes}）"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": main_text[:_SLACK_TEXT_MAX],
            },
        }
    ]

    # サムネイル画像 (cover_url がある場合)
    if video.cover_url:
        blocks[0]["accessory"] = {
            "type": "image",
            "image_url": video.cover_url,
            "alt_text": f"@{video.author_handle} のサムネイル",
        }

    # AI 分析セクション (説明・タグを太字、絵文字なし、モデル名なし)
    if ai_result and "error" not in ai_result:
        ai_lines = []
        desc_val = ai_result.get("visual_description") or ai_result.get("description", "")
        if desc_val:
            ai_lines.append(f"*説明:* {desc_val[:200]}")
        if ai_result.get("category"):
            ai_lines.append(f"カテゴリ: {ai_result['category']}")
        if ai_result.get("trend_reason"):
            ai_lines.append(f"バズ理由: {ai_result['trend_reason'][:120]}")
        if ai_result.get("emotion"):
            ai_lines.append(f"感情: {ai_result['emotion']}")
        # 実際のハッシュタグを優先、なければ AI 提案
        display_tags = real_tags or ai_result.get("tags", [])
        if display_tags:
            tag_str = "  ".join(f"`#{t}`" for t in display_tags[:8])
            ai_lines.append(f"*タグ:* {tag_str}")

        if ai_lines:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "\n".join(ai_lines)[:_SLACK_TEXT_MAX],
                    }
                ],
            })

    blocks.append({"type": "divider"})
    return blocks


def _build_header_blocks(
    total: int,
    jp_count: int,
    send_count: int,
    collected_at: datetime,
) -> list[dict]:
    """レポートのヘッダーブロックを構築する.

    Args:
        total: 収集合計件数.
        jp_count: 日本語コンテンツ件数.
        send_count: Slack に送信する件数.
        collected_at: 収集日時.

    Returns:
        Slack Block Kit ブロックのリスト.
    """
    jst_time = collected_at.astimezone(timezone.utc)
    time_str = jst_time.strftime("%Y-%m-%d %H:%M UTC")

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🇯🇵 TikTok FYP トレンドレポート",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*収集件数:*\n{total} 件"},
                {"type": "mrkdwn", "text": f"*日本語率:*\n{jp_count}/{total} ({jp_count*100//total if total else 0}%)"},
                {"type": "mrkdwn", "text": f"*通知件数:*\n上位 {send_count} 件"},
                {"type": "mrkdwn", "text": f"*収集時刻:*\n{time_str}"},
            ],
        },
        {"type": "divider"},
    ]


class SlackNotifier:
    """Slack Incoming Webhook で TikTok トレンドを通知するクラス.

    Attributes:
        _webhook_url: Slack Incoming Webhook URL.
    """

    def __init__(self, webhook_url: str) -> None:
        """コンストラクタ.

        Args:
            webhook_url: Slack Incoming Webhook URL.
                例: "https://hooks.slack.com/services/T.../B.../..."
        """
        self._webhook_url = webhook_url

    async def send_trend_report(
        self,
        videos: list["FYPVideo"],
        ai_results: dict[str, dict[str, Any]],
        top_n: int = 10,
    ) -> bool:
        """FYP 収集結果をトレンドレポートとして Slack に送信する.

        動画件数が多い場合は複数メッセージに分割して送信する。

        Args:
            videos: 収集した FYPVideo のリスト (いいね数降順推奨).
            ai_results: {video_id: Gemini分析結果} の辞書.
            top_n: 送信する上位件数 (default: 10).

        Returns:
            全メッセージが成功した場合 True.
        """
        targets = videos[:top_n]
        if not targets:
            log.warning("slack_no_videos_to_send")
            return False

        jp_count = sum(1 for v in videos if v.is_japanese())
        collected_at = targets[0].collected_at if targets else datetime.now(timezone.utc)

        # ヘッダーブロック
        all_blocks: list[dict] = _build_header_blocks(
            total=len(videos),
            jp_count=jp_count,
            send_count=len(targets),
            collected_at=collected_at,
        )

        # 各動画のブロック
        for rank, video in enumerate(targets, 1):
            ai = ai_results.get(video.video_id)
            all_blocks.extend(_build_video_block(video, rank, ai))

        # Slack の上限に合わせて分割送信
        success = True
        chunk_start = 0
        while chunk_start < len(all_blocks):
            chunk = all_blocks[chunk_start : chunk_start + _MAX_BLOCKS_PER_MESSAGE]
            ok = await self._post({"blocks": chunk})
            if not ok:
                success = False
            chunk_start += _MAX_BLOCKS_PER_MESSAGE
            if chunk_start < len(all_blocks):
                await asyncio.sleep(1)  # Slack レート制限対応

        return success

    async def send_simple(self, text: str) -> bool:
        """シンプルなテキストメッセージを送信する.

        Args:
            text: 送信するテキスト.

        Returns:
            成功した場合 True.
        """
        return await self._post({"text": text})

    async def _post(self, payload: dict) -> bool:
        """Webhook URL に JSON ペイロードを POST する.

        Args:
            payload: 送信する辞書 (JSON にシリアライズされる).

        Returns:
            HTTP 200 が返った場合 True.
        """
        import urllib.error
        import urllib.request

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        loop = asyncio.get_event_loop()
        try:
            def _do_post() -> bool:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    ok = resp.status == 200
                    if not ok:
                        log.warning("slack_post_failed", status=resp.status)
                    return ok

            result = await loop.run_in_executor(None, _do_post)
            if result:
                log.info("slack_post_ok")
            return result

        except urllib.error.HTTPError as exc:
            body_preview = exc.read(200).decode("utf-8", errors="replace")
            log.error("slack_http_error", status=exc.code, body=body_preview)
            return False
        except Exception as exc:
            log.error("slack_post_error", error=str(exc))
            return False
