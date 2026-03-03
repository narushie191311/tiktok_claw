"""デイリーレポート要約モジュール.

収集済みツイートと分析結果をもとに、
構造化されたデイリーレポートを LLM で自動生成する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from Iran_ocint.analysis.llm_client import BaseLLMClient
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

REPORT_SYSTEM_PROMPT = """You are a senior OSINT analyst producing a daily intelligence briefing 
on Iran. Generate a structured report in Markdown format.

The report should follow this structure:

## Executive Summary
2-3 paragraphs summarizing the most significant developments.

## Geopolitics & Diplomacy
Key developments in Iran's international relations, sanctions, nuclear negotiations.

## Military & Security
IRGC activities, missile developments, proxy force operations, regional conflicts.

## Economy & Energy
Oil markets, sanctions impact, currency trends, trade developments.

## Domestic Affairs
Internal politics, protests, human rights, governance changes.

## Cyber & Information Warfare
Cyber operations, disinformation campaigns, digital surveillance.

## Key Accounts & Emerging Voices
Notable accounts that gained traction or new accounts worth monitoring.

## Outlook
Brief forward-looking assessment of what to watch in the next 24-48 hours.

Guidelines:
- Be analytical, not just descriptive — provide context and implications
- Use bullet points for clarity within sections
- Include specific data points (engagement counts, timing) when relevant
- Flag items requiring immediate attention with [PRIORITY]
- Write in English
- Be concise but thorough"""

DAILY_REPORT_USER_TEMPLATE = """Generate the daily Iran OSINT briefing for {date}.

Total posts analyzed: {total_count}
Breaking events detected: {breaking_count}

## Topic Distribution
{topic_distribution}

## Top Posts by Engagement
{top_posts}

## Breaking Events Summary
{breaking_events}

## Raw Post Samples by Topic
{topic_samples}
"""


@dataclass
class ReportInput:
    """レポート生成に必要な入力データ.

    Attributes:
        date: レポート対象日.
        total_count: 分析済みツイート総数.
        breaking_count: 速報イベント件数.
        topic_distribution: トピック別件数辞書.
        top_posts: エンゲージメント上位のポスト.
        breaking_events: 検知された速報イベント.
        topic_samples: トピック別のサンプルポスト.
    """

    date: str
    total_count: int = 0
    breaking_count: int = 0
    topic_distribution: dict[str, int] = field(default_factory=dict)
    top_posts: list[dict] = field(default_factory=list)
    breaking_events: list[dict] = field(default_factory=list)
    topic_samples: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class GeneratedReport:
    """生成されたレポート.

    Attributes:
        markdown: Markdown 形式のレポート本文.
        date: レポート対象日.
        total_count: 分析済みツイート総数.
        breaking_count: 速報イベント件数.
        generated_at: 生成日時.
    """

    markdown: str
    date: str
    total_count: int
    breaking_count: int
    generated_at: datetime


async def generate_daily_report(
    llm: BaseLLMClient,
    report_input: ReportInput,
) -> GeneratedReport:
    """デイリーレポートを LLM で自動生成する.

    Args:
        llm: LLM クライアントインスタンス.
        report_input: レポート生成用の入力データ.

    Returns:
        GeneratedReport インスタンス.
    """
    topic_dist_text = "\n".join(
        f"- {topic}: {count} posts"
        for topic, count in sorted(
            report_input.topic_distribution.items(), key=lambda x: x[1], reverse=True
        )
    ) or "No data available"

    top_posts_text = _format_top_posts(report_input.top_posts)
    breaking_text = _format_breaking_events(report_input.breaking_events)
    samples_text = _format_topic_samples(report_input.topic_samples)

    user_prompt = DAILY_REPORT_USER_TEMPLATE.format(
        date=report_input.date,
        total_count=report_input.total_count,
        breaking_count=report_input.breaking_count,
        topic_distribution=topic_dist_text,
        top_posts=top_posts_text,
        breaking_events=breaking_text,
        topic_samples=samples_text,
    )

    try:
        resp = await llm.complete(
            system_prompt=REPORT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=4096,
        )

        report_md = resp.text.strip()

        header = (
            f"# Iran OSINT Daily Report — {report_input.date}\n\n"
            f"*Generated at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} | "
            f"{report_input.total_count} posts analyzed | "
            f"{report_input.breaking_count} breaking events*\n\n---\n\n"
        )

        full_report = header + report_md

        log.info(
            "daily_report_generated",
            date=report_input.date,
            chars=len(full_report),
            tokens_used=resp.usage_prompt_tokens + resp.usage_completion_tokens,
        )

        return GeneratedReport(
            markdown=full_report,
            date=report_input.date,
            total_count=report_input.total_count,
            breaking_count=report_input.breaking_count,
            generated_at=datetime.now(UTC),
        )

    except Exception as exc:
        log.error("report_generation_failed", error=str(exc))
        fallback_md = (
            f"# Iran OSINT Daily Report — {report_input.date}\n\n"
            f"**Report generation failed**: {exc}\n\n"
            f"Total posts: {report_input.total_count}\n"
            f"Breaking events: {report_input.breaking_count}\n"
        )
        return GeneratedReport(
            markdown=fallback_md,
            date=report_input.date,
            total_count=report_input.total_count,
            breaking_count=report_input.breaking_count,
            generated_at=datetime.now(UTC),
        )


def _format_top_posts(posts: list[dict]) -> str:
    """上位ポストをフォーマットする.

    Args:
        posts: ポスト辞書のリスト (text, author, engagement キーを含む).

    Returns:
        フォーマット済み文字列.
    """
    if not posts:
        return "No top posts available"

    lines: list[str] = []
    for i, p in enumerate(posts[:10], 1):
        author = p.get("author", "unknown")
        text = p.get("text", "")[:200]
        eng = p.get("engagement", 0)
        lines.append(f"{i}. @{author} (engagement: {eng})\n   {text}")
    return "\n\n".join(lines)


def _format_breaking_events(events: list[dict]) -> str:
    """速報イベントをフォーマットする.

    Args:
        events: イベント辞書のリスト.

    Returns:
        フォーマット済み文字列.
    """
    if not events:
        return "No breaking events detected"

    lines: list[str] = []
    for e in events:
        headline = e.get("headline", "Unknown event")
        severity = e.get("severity", 0.0)
        assessment = e.get("assessment", "")
        lines.append(f"- [{severity:.1f}] {headline}\n  {assessment}")
    return "\n\n".join(lines)


def _format_topic_samples(samples: dict[str, list[str]]) -> str:
    """トピック別サンプルをフォーマットする.

    Args:
        samples: トピック→テキストリストの辞書.

    Returns:
        フォーマット済み文字列.
    """
    if not samples:
        return "No samples available"

    lines: list[str] = []
    for topic, texts in samples.items():
        lines.append(f"### {topic.title()}")
        for t in texts[:3]:
            lines.append(f"- {t[:200]}")
        lines.append("")
    return "\n".join(lines)
