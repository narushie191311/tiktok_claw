"""通知配信の抽象基底クラス.

全通知チャンネル (Slack, Email) が実装すべき共通インターフェースを定義する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AbstractNotifier(ABC):
    """通知配信の抽象インターフェース."""

    @abstractmethod
    async def send_alert(self, headline: str, body: str, severity: float = 0.0) -> bool:
        """速報アラートを送信する.

        Args:
            headline: アラート見出し.
            body: アラート本文.
            severity: 重大度 (0.0 ~ 1.0).

        Returns:
            送信成功なら True.
        """
        ...

    @abstractmethod
    async def send_report(self, title: str, markdown_body: str) -> bool:
        """デイリーレポートを送信する.

        Args:
            title: レポートタイトル.
            markdown_body: Markdown 形式のレポート本文.

        Returns:
            送信成功なら True.
        """
        ...
