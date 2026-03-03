"""分析パイプライン統合モジュール.

収集 → 翻訳 → 分類 → イベント検知 → 保存 の一連のフローを
単一のエントリポイントで実行する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from analysis.classifier import ClassificationResult, classify_text
from analysis.event_detector import (
    BreakingEvent,
    detect_spike,
    should_alert,
    triage_event,
)
from analysis.llm_client import BaseLLMClient
from analysis.summarizer import GeneratedReport, ReportInput, generate_daily_report
from analysis.translator import translate_text
from collectors.base import CollectedPost
from storage.database import (
    get_hourly_counts,
    get_session,
    get_tweets_since,
    store_analysis,
    upsert_tweet,
)
from utils.logger import get_logger

log = get_logger(__name__)


async def run_analysis_pipeline(
    llm: BaseLLMClient,
    posts: list[CollectedPost],
    severity_threshold: float = 0.7,
    sigma_threshold: float = 3.0,
) -> tuple[list[ClassificationResult], list[BreakingEvent]]:
    """収集済みポストに対して分析パイプライン全体を実行する.

    処理フロー:
        1. DB にツイートを保存 (重複スキップ)
        2. 非英語テキストを翻訳
        3. トピック分類 + 感情/重大度スコアリング
        4. 分析結果を DB に保存
        5. 頻度スパイク検知
        6. スパイク検知時に LLM トリアージ

    Args:
        llm: LLM クライアントインスタンス.
        posts: 収集されたポストのリスト.
        severity_threshold: アラート発報の重大度閾値.
        sigma_threshold: スパイク検知の σ 閾値.

    Returns:
        (分類結果リスト, 検知された速報イベントリスト) のタプル.
    """
    if not posts:
        log.info("pipeline_skipped", reason="no posts to analyze")
        return [], []

    log.info("pipeline_started", post_count=len(posts))

    all_classifications: list[ClassificationResult] = []
    breaking_events: list[BreakingEvent] = []
    new_post_count = 0

    async with get_session() as session:
        for post in posts:
            # 1. DB 保存 (重複排除)
            tweet = await upsert_tweet(session, post.to_db_dict())
            if tweet is None:
                continue
            new_post_count += 1

            # 2. 翻訳 (非英語)
            translated = post.text
            if post.lang not in ("en", "und"):
                translated = await translate_text(
                    llm, post.text, source_lang=post.lang, target_lang="en"
                )
                tweet.text_translated = translated

            # 3. 分類
            text_for_analysis = translated
            classification = await classify_text(llm, text_for_analysis)
            all_classifications.append(classification)

            # 4. 分析結果保存
            await store_analysis(session, {
                "tweet_id": post.post_id,
                "topic": classification.topic,
                "sentiment_score": classification.sentiment_score,
                "severity_score": classification.severity_score,
                "is_breaking": classification.is_breaking,
                "summary": classification.summary,
                "keywords_matched": json.dumps(classification.key_entities),
            })

        # 5. スパイク検知
        hourly_counts = await get_hourly_counts(session, hours=24)
        current_hour_count = new_post_count
        spike_info = detect_spike(hourly_counts, current_hour_count, sigma_threshold)

        # 6. スパイク検知時 → LLM トリアージ
        if spike_info.is_spike and all_classifications:
            texts = [p.text for p in posts[:20]]
            event = await triage_event(llm, texts, all_classifications, spike_info)

            if should_alert(event, severity_threshold):
                breaking_events.append(event)
                log.warning(
                    "breaking_event_detected",
                    headline=event.headline,
                    severity=event.severity,
                    event_type=event.event_type,
                )

        # 個別ポストの is_breaking もチェック
        for i, cls in enumerate(all_classifications):
            if cls.is_breaking and cls.severity_score >= severity_threshold:
                single_event = BreakingEvent(
                    is_significant=True,
                    event_type=cls.topic,
                    severity=cls.severity_score,
                    headline=cls.summary,
                    assessment=cls.summary,
                    spike_info=None,
                    trigger_posts=[posts[i].text[:500]] if i < len(posts) else [],
                    detected_at=datetime.now(UTC),
                )
                breaking_events.append(single_event)

    log.info(
        "pipeline_completed",
        new_posts=new_post_count,
        classifications=len(all_classifications),
        breaking_events=len(breaking_events),
    )

    return all_classifications, breaking_events


async def run_daily_report(
    llm: BaseLLMClient,
    report_date: str | None = None,
) -> GeneratedReport:
    """デイリーレポートを生成する.

    過去24時間のデータを集計し、LLM でレポートを自動生成する。

    Args:
        llm: LLM クライアントインスタンス.
        report_date: レポート対象日 ("YYYY-MM-DD"). None なら当日.

    Returns:
        GeneratedReport インスタンス.
    """
    if report_date is None:
        report_date = datetime.now(UTC).strftime("%Y-%m-%d")

    since = datetime.now(UTC) - timedelta(hours=24)

    async with get_session() as session:
        tweets = await get_tweets_since(session, since)

    # トピック別集計
    topic_distribution: dict[str, int] = {}
    topic_samples: dict[str, list[str]] = {}
    breaking_events_data: list[dict] = []
    top_posts: list[dict] = []

    for tweet in tweets:
        # エンゲージメントスコアで上位ポストを抽出
        engagement = tweet.retweet_count + tweet.like_count + tweet.reply_count
        top_posts.append({
            "author": tweet.author_handle,
            "text": tweet.text_translated or tweet.text,
            "engagement": engagement,
        })

    # エンゲージメント順にソート
    top_posts.sort(key=lambda x: x["engagement"], reverse=True)
    top_posts = top_posts[:10]

    # DB から分析結果を取得してトピック集計
    async with get_session() as session:
        from sqlalchemy import select
        from storage.models import AnalysisResult

        for tweet in tweets:
            result = await session.execute(
                select(AnalysisResult).where(AnalysisResult.tweet_id == tweet.tweet_id)
            )
            analysis = result.scalar_one_or_none()
            if analysis:
                topic = analysis.topic
                topic_distribution[topic] = topic_distribution.get(topic, 0) + 1
                if topic not in topic_samples:
                    topic_samples[topic] = []
                if len(topic_samples[topic]) < 5:
                    topic_samples[topic].append(
                        tweet.text_translated or tweet.text
                    )

                if analysis.is_breaking:
                    breaking_events_data.append({
                        "headline": analysis.summary,
                        "severity": analysis.severity_score,
                        "assessment": analysis.summary,
                    })

    report_input = ReportInput(
        date=report_date,
        total_count=len(tweets),
        breaking_count=len(breaking_events_data),
        topic_distribution=topic_distribution,
        top_posts=top_posts,
        breaking_events=breaking_events_data,
        topic_samples=topic_samples,
    )

    report = await generate_daily_report(llm, report_input)

    # レポートを DB に保存
    from storage.database import store_report

    async with get_session() as session:
        await store_report(session, {
            "report_date": report_date,
            "report_type": "daily",
            "content_markdown": report.markdown,
            "content_html": "",
            "tweet_count": report.total_count,
            "breaking_count": report.breaking_count,
        })

    log.info("daily_report_saved", date=report_date, tweet_count=len(tweets))
    return report
