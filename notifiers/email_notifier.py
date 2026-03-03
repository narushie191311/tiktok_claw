"""Email SMTP 通知モジュール.

SMTP (Gmail / SES 等) 経由で速報アラートとデイリーレポートをメール配信する。
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from Iran_ocint.notifiers.base import AbstractNotifier
from Iran_ocint.notifiers.report_formatter import markdown_to_html
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)


class EmailNotifier(AbstractNotifier):
    """SMTP メール通知.

    Attributes:
        _host: SMTP サーバーホスト.
        _port: SMTP ポート.
        _user: SMTP 認証ユーザー.
        _password: SMTP 認証パスワード.
        _recipients: 送信先メールアドレスリスト.
        _use_tls: TLS を使用するか.
        _enabled: 通知有効フラグ.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        recipients: list[str] | None = None,
        use_tls: bool = True,
    ) -> None:
        """コンストラクタ.

        Args:
            host: SMTP ホスト. None なら環境変数 SMTP_HOST.
            port: SMTP ポート. None なら環境変数 SMTP_PORT.
            user: SMTP ユーザー. None なら環境変数 SMTP_USER.
            password: SMTP パスワード. None なら環境変数 SMTP_PASSWORD.
            recipients: 送信先リスト. None なら環境変数 EMAIL_RECIPIENTS (カンマ区切り).
            use_tls: TLS を使用するか.
        """
        self._host = host or os.getenv("SMTP_HOST", "smtp.gmail.com")
        self._port = port or int(os.getenv("SMTP_PORT", "587"))
        self._user = user or os.getenv("SMTP_USER", "")
        self._password = password or os.getenv("SMTP_PASSWORD", "")
        self._use_tls = use_tls

        if recipients:
            self._recipients = recipients
        else:
            raw = os.getenv("EMAIL_RECIPIENTS", "")
            self._recipients = [r.strip() for r in raw.split(",") if r.strip()]

        self._enabled = bool(self._user and self._password and self._recipients)

        if not self._enabled:
            log.warning(
                "email_notifier_disabled",
                reason="Missing SMTP credentials or recipients",
            )

    async def send_alert(self, headline: str, body: str, severity: float = 0.0) -> bool:
        """速報アラートをメール送信する.

        Args:
            headline: アラート見出し.
            body: アラート本文.
            severity: 重大度 (0.0 ~ 1.0).

        Returns:
            送信成功なら True.
        """
        if not self._enabled:
            log.info("email_alert_skipped", reason="disabled")
            return False

        severity_tag = "CRITICAL" if severity >= 0.9 else "HIGH" if severity >= 0.7 else "ALERT"
        subject = f"[{severity_tag}] Iran OSINT: {headline}"

        md_body = f"# {headline}\n\n**Severity:** {severity:.1f}\n\n{body}"
        html_body = markdown_to_html(md_body)

        return self._send_email(subject, md_body, html_body)

    async def send_report(self, title: str, markdown_body: str) -> bool:
        """デイリーレポートをメール送信する.

        Args:
            title: レポートタイトル.
            markdown_body: Markdown 形式のレポート本文.

        Returns:
            送信成功なら True.
        """
        if not self._enabled:
            log.info("email_report_skipped", reason="disabled")
            return False

        subject = f"[Daily Report] {title}"
        html_body = markdown_to_html(markdown_body)

        return self._send_email(subject, markdown_body, html_body)

    def _send_email(self, subject: str, text_body: str, html_body: str) -> bool:
        """SMTP でメールを送信する.

        Args:
            subject: メール件名.
            text_body: プレーンテキスト本文.
            html_body: HTML 本文.

        Returns:
            送信成功なら True.
        """
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self._user
            msg["To"] = ", ".join(self._recipients)

            msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(self._host, self._port) as server:
                if self._use_tls:
                    server.starttls()
                server.login(self._user, self._password)
                server.sendmail(self._user, self._recipients, msg.as_string())

            log.info(
                "email_sent",
                subject=subject,
                recipients=len(self._recipients),
            )
            return True

        except Exception as exc:
            log.error("email_send_failed", error=str(exc), subject=subject)
            return False
