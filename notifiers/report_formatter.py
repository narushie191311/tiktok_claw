"""レポートフォーマッターモジュール.

Markdown → HTML 変換と Jinja2 テンプレートによるレポート生成を提供する。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
    color: #1a1a2e;
    background: #f5f5f5;
    line-height: 1.6;
  }}
  .container {{
    background: white;
    border-radius: 12px;
    padding: 32px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  }}
  h1 {{
    color: #16213e;
    border-bottom: 3px solid #0f3460;
    padding-bottom: 12px;
    font-size: 1.5em;
  }}
  h2 {{
    color: #0f3460;
    margin-top: 28px;
    font-size: 1.2em;
    border-left: 4px solid #e94560;
    padding-left: 12px;
  }}
  h3 {{
    color: #533483;
    font-size: 1em;
  }}
  .meta {{
    color: #666;
    font-size: 0.85em;
    margin-bottom: 20px;
  }}
  .priority {{
    background: #e94560;
    color: white;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: bold;
    font-size: 0.85em;
  }}
  ul {{
    padding-left: 20px;
  }}
  li {{
    margin-bottom: 6px;
  }}
  blockquote {{
    border-left: 3px solid #ddd;
    margin-left: 0;
    padding-left: 16px;
    color: #555;
  }}
  code {{
    background: #f0f0f0;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
  }}
  hr {{
    border: none;
    border-top: 1px solid #eee;
    margin: 24px 0;
  }}
  .footer {{
    text-align: center;
    color: #999;
    font-size: 0.8em;
    margin-top: 32px;
  }}
</style>
</head>
<body>
<div class="container">
{content}
<hr>
<div class="footer">
  Iran_ocint — Iran OSINT Monitoring System<br>
  Generated at {timestamp}
</div>
</div>
</body>
</html>"""


def markdown_to_html(markdown_text: str) -> str:
    """簡易 Markdown → HTML 変換.

    完全な Markdown パーサーではなく、レポートで使用する主要な要素
    (見出し、リスト、太字、斜体、水平線) に対応する。

    Args:
        markdown_text: Markdown 形式テキスト.

    Returns:
        HTML 文字列.
    """
    lines = markdown_text.split("\n")
    html_lines: list[str] = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        # 空行
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue

        # 水平線
        if stripped in ("---", "***", "___"):
            html_lines.append("<hr>")
            continue

        # 見出し
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_inline_format(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            html_lines.append(f"<h2>{_inline_format(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{_inline_format(stripped[2:])}</h1>")
            continue

        # リスト項目
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = _inline_format(stripped[2:])
            html_lines.append(f"<li>{content}</li>")
            continue

        # 通常段落
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        html_lines.append(f"<p>{_inline_format(stripped)}</p>")

    if in_list:
        html_lines.append("</ul>")

    content = "\n".join(html_lines)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return HTML_TEMPLATE.format(content=content, timestamp=timestamp)


def _inline_format(text: str) -> str:
    """インラインフォーマット (太字、斜体、コード、PRIORITY タグ) を変換する.

    Args:
        text: 入力テキスト.

    Returns:
        HTML インライン要素に変換されたテキスト.
    """
    # [PRIORITY] タグ
    text = text.replace("[PRIORITY]", '<span class="priority">PRIORITY</span>')

    # 太字 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    # 斜体 *text*
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # インラインコード `code`
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    return text


def format_breaking_alert_markdown(
    headline: str,
    severity: float,
    event_type: str,
    assessment: str,
    trigger_posts: list[str],
) -> str:
    """速報アラートを Markdown フォーマットする.

    Args:
        headline: 見出し.
        severity: 重大度.
        event_type: イベント種別.
        assessment: 状況分析.
        trigger_posts: トリガーポスト群.

    Returns:
        Markdown フォーマットされたアラート文字列.
    """
    label = "CRITICAL" if severity >= 0.9 else "HIGH" if severity >= 0.7 else "MEDIUM"

    parts = [
        f"**[{label}] {headline}**",
        "",
        f"**Type:** {event_type} | **Severity:** {severity:.1f}",
        "",
        f"**Assessment:** {assessment}",
    ]

    if trigger_posts:
        parts.append("")
        parts.append("**Key Posts:**")
        for i, post in enumerate(trigger_posts[:5], 1):
            parts.append(f"{i}. {post[:200]}")

    return "\n".join(parts)
