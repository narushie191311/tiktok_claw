"""Slack Incoming Webhook 通知モジュール.

Slack Webhook URL を使って速報アラートとデイリーレポートを配信する。
"""

from __future__ import annotations

import os

from Iran_ocint.notifiers.base import AbstractNotifier
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

# 重大度に応じた色コード
SEVERITY_COLORS = {
    "critical": "#FF0000",   # 赤
    "high": "#FF6600",       # オレンジ
    "medium": "#FFCC00",     # 黄
    "low": "#00CC66",        # 緑
    "info": "#0066CC",       # 青
}


class SlackNotifier(AbstractNotifier):
    """Slack Incoming Webhook による通知配信.

    Attributes:
        _webhook_url: Slack Webhook URL.
        _enabled: 通知有効フラグ.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        """コンストラクタ.

        Args:
            webhook_url: Slack Webhook URL. None なら環境変数 SLACK_WEBHOOK_URL を使用.
        """
        self._webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
        self._enabled = bool(self._webhook_url)

        if not self._enabled:
            log.warning("slack_notifier_disabled", reason="No webhook URL configured")

    async def send_alert(self, headline: str, body: str, severity: float = 0.0) -> bool:
        """速報アラートを Slack に送信する.

        Args:
            headline: アラート見出し.
            body: アラート本文.
            severity: 重大度 (0.0 ~ 1.0).

        Returns:
            送信成功なら True.
        """
        if not self._enabled:
            log.info("slack_alert_skipped", reason="disabled")
            return False

        color = _severity_to_color(severity)
        severity_label = _severity_to_label(severity)

        payload = {
            "text": f"🚨 Iran OSINT Alert: {headline}",
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"🚨 {headline}",
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Severity:* {severity_label} ({severity:.1f})",
                                },
                            ],
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": body[:2900],
                            },
                        },
                    ],
                }
            ],
        }

        return await self._send(payload)

    async def send_report(self, title: str, markdown_body: str) -> bool:
        """デイリーレポートを Slack に送信する.

        Slack の文字数制限 (3000字) を考慮し、長文は分割送信する。

        Args:
            title: レポートタイトル.
            markdown_body: Markdown 形式のレポート本文.

        Returns:
            送信成功なら True.
        """
        if not self._enabled:
            log.info("slack_report_skipped", reason="disabled")
            return False

        chunks = _split_text(markdown_body, max_len=2900)
        success = True

        for i, chunk in enumerate(chunks):
            if i == 0:
                payload = {
                    "text": f"📊 {title}",
                    "blocks": [
                        {
                            "type": "header",
                            "text": {"type": "plain_text", "text": f"📊 {title}"},
                        },
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": chunk},
                        },
                    ],
                }
            else:
                payload = {
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"_(continued {i + 1}/{len(chunks)})_\n\n{chunk}",
                            },
                        },
                    ],
                }

            if not await self._send(payload):
                success = False

        return success

    async def _send(self, payload: dict) -> bool:
        """Webhook にペイロードを POST する.

        Args:
            payload: Slack メッセージペイロード.

        Returns:
            送信成功なら True.
        """
        try:
            from slack_sdk.webhook.async_client import AsyncWebhookClient

            client = AsyncWebhookClient(self._webhook_url)
            response = await client.send_dict(payload)

            if response.status_code == 200:
                log.info("slack_message_sent")
                return True
            else:
                log.error(
                    "slack_send_failed",
                    status=response.status_code,
                    body=response.body,
                )
                return False

        except Exception as exc:
            log.error("slack_send_error", error=str(exc))
            return False


def _severity_to_color(severity: float) -> str:
    """重大度を Slack 色コードに変換する.

    Args:
        severity: 重大度 (0.0 ~ 1.0).

    Returns:
        HTML 色コード文字列.
    """
    if severity >= 0.9:
        return SEVERITY_COLORS["critical"]
    elif severity >= 0.7:
        return SEVERITY_COLORS["high"]
    elif severity >= 0.5:
        return SEVERITY_COLORS["medium"]
    elif severity >= 0.3:
        return SEVERITY_COLORS["low"]
    return SEVERITY_COLORS["info"]


def _severity_to_label(severity: float) -> str:
    """重大度をラベル文字列に変換する.

    Args:
        severity: 重大度 (0.0 ~ 1.0).

    Returns:
        重大度ラベル ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO").
    """
    if severity >= 0.9:
        return "CRITICAL"
    elif severity >= 0.7:
        return "HIGH"
    elif severity >= 0.5:
        return "MEDIUM"
    elif severity >= 0.3:
        return "LOW"
    return "INFO"


def _split_text(text: str, max_len: int = 2900) -> list[str]:
    """長文テキストを Slack の文字数制限に合わせて分割する.

    段落境界 (空行) を優先して分割する。

    Args:
        text: 分割対象テキスト.
        max_len: 1チャンクの最大文字数.

    Returns:
        分割されたテキストのリスト.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > max_len:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = para
        else:
            current_chunk = f"{current_chunk}\n\n{para}" if current_chunk else para

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks or [text[:max_len]]
