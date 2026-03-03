"""イベント検知モジュール.

頻度スパイク検知 + LLM トリアージにより、
速報レベルのイベントをリアルタイムで検出する。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime

from analysis.classifier import ClassificationResult
from analysis.llm_client import BaseLLMClient
from utils.logger import get_logger

log = get_logger(__name__)

TRIAGE_SYSTEM_PROMPT = """You are a senior intelligence analyst. You are evaluating whether 
a cluster of social media posts represents a significant breaking event related to Iran.

Evaluate the posts and return a JSON object:
{
  "is_significant": boolean — true if this is a genuinely significant event,
  "event_type": string — brief label (e.g., "military strike", "sanctions announcement", "protest"),
  "severity": float from 0.0 to 1.0,
  "headline": string — one-line headline for the alert,
  "assessment": string — 2-3 sentence analysis of the situation
}

Only mark is_significant=true for genuinely important events, not routine news coverage.
Return ONLY the JSON object."""


@dataclass
class SpikeInfo:
    """頻度スパイク検知結果.

    Attributes:
        is_spike: スパイクが検知されたか.
        current_count: 直近1時間の件数.
        mean_count: 過去24時間の平均件数/時.
        std_dev: 標準偏差.
        z_score: Zスコア.
    """

    is_spike: bool
    current_count: int
    mean_count: float
    std_dev: float
    z_score: float


@dataclass
class BreakingEvent:
    """速報イベント検知結果.

    Attributes:
        is_significant: 重大イベントか否か.
        event_type: イベント種別ラベル.
        severity: 重大度 (0.0 ~ 1.0).
        headline: アラート見出し.
        assessment: 状況分析テキスト.
        spike_info: スパイク検知情報.
        trigger_posts: トリガーとなったポスト群.
        detected_at: 検知日時.
    """

    is_significant: bool
    event_type: str
    severity: float
    headline: str
    assessment: str
    spike_info: SpikeInfo | None
    trigger_posts: list[str]
    detected_at: datetime


def detect_spike(
    hourly_counts: list[int],
    current_hour_count: int,
    sigma_threshold: float = 3.0,
) -> SpikeInfo:
    """直近1時間のツイート頻度が過去データに対してスパイクか判定する.

    Args:
        hourly_counts: 過去24時間の1時間ごとのツイート件数 (古い順).
        current_hour_count: 直近1時間の件数.
        sigma_threshold: スパイク判定の σ 閾値.

    Returns:
        SpikeInfo インスタンス.
    """
    if not hourly_counts or len(hourly_counts) < 3:
        return SpikeInfo(
            is_spike=False,
            current_count=current_hour_count,
            mean_count=0.0,
            std_dev=0.0,
            z_score=0.0,
        )

    mean = statistics.mean(hourly_counts)
    stdev = statistics.stdev(hourly_counts) if len(hourly_counts) > 1 else 0.0

    # 標準偏差が0の場合(全時間帯同数)、件数が平均の2倍以上でスパイク扱い
    if stdev == 0:
        z_score = float(current_hour_count - mean) if mean > 0 else 0.0
        is_spike = current_hour_count > mean * 2
    else:
        z_score = (current_hour_count - mean) / stdev
        is_spike = z_score >= sigma_threshold

    if is_spike:
        log.warning(
            "spike_detected",
            current=current_hour_count,
            mean=round(mean, 1),
            stdev=round(stdev, 1),
            z_score=round(z_score, 2),
        )

    return SpikeInfo(
        is_spike=is_spike,
        current_count=current_hour_count,
        mean_count=round(mean, 2),
        std_dev=round(stdev, 2),
        z_score=round(z_score, 2),
    )


async def triage_event(
    llm: BaseLLMClient,
    posts: list[str],
    classifications: list[ClassificationResult],
    spike_info: SpikeInfo | None = None,
) -> BreakingEvent:
    """LLM を使ってポスト群が重大イベントか判定する.

    Args:
        llm: LLM クライアントインスタンス.
        posts: ツイートテキストのリスト.
        classifications: 各ツイートの分類結果.
        spike_info: スパイク検知情報 (あれば).

    Returns:
        BreakingEvent インスタンス.
    """
    # 重大度が高いポストを優先的に含める
    scored_posts = list(zip(posts, classifications))
    scored_posts.sort(key=lambda x: x[1].severity_score, reverse=True)
    top_posts = [p for p, _ in scored_posts[:15]]

    context = "\n---\n".join(top_posts)
    prompt = f"Evaluate whether these posts represent a breaking event:\n\n{context}"

    try:
        data = await llm.complete_json(
            system_prompt=TRIAGE_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.1,
        )

        return BreakingEvent(
            is_significant=bool(data.get("is_significant", False)),
            event_type=data.get("event_type", "unknown"),
            severity=float(data.get("severity", 0.0)),
            headline=data.get("headline", ""),
            assessment=data.get("assessment", ""),
            spike_info=spike_info,
            trigger_posts=top_posts[:5],
            detected_at=datetime.now(UTC),
        )

    except Exception as exc:
        log.error("triage_failed", error=str(exc))
        return BreakingEvent(
            is_significant=False,
            event_type="error",
            severity=0.0,
            headline="Triage failed",
            assessment=str(exc),
            spike_info=spike_info,
            trigger_posts=top_posts[:5],
            detected_at=datetime.now(UTC),
        )


def should_alert(
    event: BreakingEvent,
    severity_threshold: float = 0.7,
) -> bool:
    """イベントがアラート送信条件を満たすか判定する.

    Args:
        event: 検知されたイベント.
        severity_threshold: 重大度の閾値.

    Returns:
        True ならアラートを送信すべき.
    """
    return event.is_significant and event.severity >= severity_threshold
