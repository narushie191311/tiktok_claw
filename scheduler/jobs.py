"""APScheduler ジョブ定義モジュール.

15分間隔の収集ジョブ、デイリーレポートジョブ、
緊急アラートの即時通知を管理する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from analysis.llm_client import BaseLLMClient, create_llm_client
from analysis.pipeline import run_analysis_pipeline, run_daily_report
from collectors.base import AbstractCollector, CollectedPost
from collectors.instagram_collector import InstagramCollector
from collectors.rss_collector import FeedConfig, RSSCollector
from collectors.tiktok_collector import TikTokCollector
from collectors.twitter_api import TwitterAPICollector
from collectors.twitter_scraper import TwitterScraperCollector
from notifiers.base import AbstractNotifier
from notifiers.email_notifier import EmailNotifier
from notifiers.report_formatter import format_breaking_alert_markdown
from notifiers.slack_notifier import SlackNotifier
from utils.logger import get_logger

log = get_logger(__name__)


class OcintOrchestrator:
    """OSINT 監視オーケストレーター.

    コレクター、分析パイプライン、通知チャンネルを統合し、
    スケジュールジョブとして実行する。

    Attributes:
        _collectors: データコレクターリスト.
        _notifiers: 通知チャンネルリスト.
        _llm: LLM クライアント.
        _queries: 検索クエリリスト.
        _settings: アプリケーション設定辞書.
    """

    def __init__(self, settings: dict) -> None:
        """コンストラクタ.

        Args:
            settings: settings.yaml から読み込んだ設定辞書.
        """
        self._settings = settings
        self._collectors: list[AbstractCollector] = []
        self._notifiers: list[AbstractNotifier] = []
        self._llm: BaseLLMClient | None = None
        self._queries: list[str] = []
        # TikTok/Instagram 収集用ハッシュタグ (コレクター有効時にセット)
        self._social_hashtags: list[str] = []

    async def initialize(self) -> None:
        """全コンポーネントを初期化する."""
        log.info("orchestrator_initializing")

        # LLM クライアント
        llm_cfg = self._settings.get("llm", {})
        provider = llm_cfg.get("provider", "openai")
        provider_cfg = llm_cfg.get(provider, {})
        self._llm = create_llm_client(
            provider=provider,
            model=provider_cfg.get("model"),
        )
        log.info("llm_client_ready", provider=provider)

        # コレクター初期化
        twitter_backend = self._settings.get("collector", {}).get("twitter_backend", "twikit")

        if twitter_backend in ("twikit", "both"):
            scraper = TwitterScraperCollector()
            await scraper.initialize()
            self._collectors.append(scraper)

        if twitter_backend in ("tweepy", "both"):
            api_collector = TwitterAPICollector()
            await api_collector.initialize()
            self._collectors.append(api_collector)

        # RSS コレクター
        if self._settings.get("collector", {}).get("rss_enabled", True):
            rss_feeds = self._load_rss_feeds()
            if rss_feeds:
                rss = RSSCollector(feeds=rss_feeds)
                await rss.initialize()
                self._collectors.append(rss)

        # TikTok コレクター
        collector_cfg = self._settings.get("collector", {})
        if collector_cfg.get("tiktok_enabled", False):
            tiktok = TikTokCollector()
            await tiktok.initialize()
            self._collectors.append(tiktok)
            self._social_hashtags = self._load_social_hashtags()
            log.info("tiktok_collector_added", tags=len(self._social_hashtags))
        else:
            log.info("tiktok_collector_disabled", hint="Set tiktok_enabled: true in settings.yaml")

        # Instagram コレクター
        if collector_cfg.get("instagram_enabled", False):
            instagram = InstagramCollector()
            await instagram.initialize()
            self._collectors.append(instagram)
            if not hasattr(self, "_social_hashtags"):
                self._social_hashtags = self._load_social_hashtags()
            log.info("instagram_collector_added", tags=len(self._social_hashtags))
        else:
            log.info("instagram_collector_disabled", hint="Set instagram_enabled: true in settings.yaml")

        # 通知チャンネル
        self._notifiers.append(SlackNotifier())
        self._notifiers.append(EmailNotifier())

        # キーワードクエリの構築
        self._queries = self._build_queries()

        log.info(
            "orchestrator_ready",
            collectors=len(self._collectors),
            notifiers=len(self._notifiers),
            queries=len(self._queries),
        )

    def _load_social_hashtags(self) -> list[str]:
        """social_hashtags.yaml からハッシュタグリストを読み込む.

        Returns:
            ハッシュタグ文字列のリスト (# 不要).
        """
        config_path = Path("Iran_ocint/config/social_hashtags.yaml")
        if not config_path.exists():
            return ["trending", "viral", "fyp"]

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        tags: list[str] = []
        for section_tags in data.values():
            if isinstance(section_tags, list):
                tags.extend(str(t) for t in section_tags if t)

        seen: set[str] = set()
        unique: list[str] = []
        for tag in tags:
            if tag and tag not in seen:
                seen.add(tag)
                unique.append(tag)

        return unique

    def _load_rss_feeds(self) -> list[FeedConfig]:
        """accounts.yaml から RSS フィード設定を読み込む.

        Returns:
            FeedConfig のリスト.
        """
        config_path = Path("Iran_ocint/config/accounts.yaml")
        if not config_path.exists():
            return []

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        feeds: list[FeedConfig] = []
        for feed in data.get("rss_feeds", []):
            feeds.append(FeedConfig(
                url=feed["url"],
                name=feed.get("name", "Unknown"),
                lang=feed.get("lang", "en"),
            ))
        return feeds

    def _build_queries(self) -> list[str]:
        """keywords.yaml から検索クエリを構築する.

        Returns:
            Twitter 検索クエリ文字列のリスト.
        """
        config_path = Path("Iran_ocint/config/keywords.yaml")
        if not config_path.exists():
            return ["Iran", "IRGC", "JCPOA", "ایران"]

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        queries: list[str] = []
        for topic, langs in data.items():
            if isinstance(langs, dict):
                for lang, keywords in langs.items():
                    if isinstance(keywords, list):
                        queries.extend(keywords)

        # 重複排除しつつ順序保持
        seen: set[str] = set()
        unique: list[str] = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)
        return unique

    async def run_collection_cycle(self) -> None:
        """1回分の収集・分析サイクルを実行する.

        全コレクターでデータ収集 → 分析パイプライン実行 →
        速報検知時に即時通知 の一連のフローを実行する。
        """
        assert self._llm is not None
        log.info("collection_cycle_started", time=datetime.now(UTC).isoformat())

        # 全コレクターからデータ収集
        # TikTok/Instagram はハッシュタグクエリ、Twitter はキーワードクエリを使用する
        all_posts: list[CollectedPost] = []
        collector_cfg = self._settings.get("collector", {})
        max_per_query = collector_cfg.get("max_tweets_per_query", 100)
        tiktok_max = collector_cfg.get("tiktok_max_per_tag", 30)
        instagram_max = collector_cfg.get("instagram_max_per_tag", 50)

        for collector in self._collectors:
            try:
                if isinstance(collector, TikTokCollector):
                    queries = self._social_hashtags
                    max_r = tiktok_max
                elif isinstance(collector, InstagramCollector):
                    queries = self._social_hashtags
                    max_r = instagram_max
                else:
                    queries = self._queries
                    max_r = max_per_query

                posts = await collector.collect(queries, max_results=max_r)

                # TikTok トレンド動画も追加取得
                if isinstance(collector, TikTokCollector) and collector_cfg.get("tiktok_fetch_trending", True):
                    trending = await collector.get_trending_videos(count=30)
                    posts.extend(trending)

                all_posts.extend(posts)
                log.info(
                    "collector_done",
                    collector=collector.__class__.__name__,
                    count=len(posts),
                )
            except Exception as exc:
                log.error(
                    "collector_failed",
                    collector=collector.__class__.__name__,
                    error=str(exc),
                )

        if not all_posts:
            log.info("collection_cycle_empty", msg="No posts collected this cycle")
            return

        # 分析パイプライン実行
        event_cfg = self._settings.get("event_detection", {})
        severity_threshold = event_cfg.get("severity_threshold", 0.7)
        sigma_threshold = event_cfg.get("spike_sigma_threshold", 3.0)

        classifications, breaking_events = await run_analysis_pipeline(
            llm=self._llm,
            posts=all_posts,
            severity_threshold=severity_threshold,
            sigma_threshold=sigma_threshold,
        )

        # 速報イベント → 即時通知
        for event in breaking_events:
            alert_md = format_breaking_alert_markdown(
                headline=event.headline,
                severity=event.severity,
                event_type=event.event_type,
                assessment=event.assessment,
                trigger_posts=event.trigger_posts,
            )

            for notifier in self._notifiers:
                try:
                    await notifier.send_alert(
                        headline=event.headline,
                        body=alert_md,
                        severity=event.severity,
                    )
                except Exception as exc:
                    log.error(
                        "alert_send_failed",
                        notifier=notifier.__class__.__name__,
                        error=str(exc),
                    )

        log.info(
            "collection_cycle_completed",
            posts=len(all_posts),
            classifications=len(classifications),
            alerts=len(breaking_events),
        )

    async def run_daily_report_job(self) -> None:
        """デイリーレポートを生成・配信するジョブ."""
        assert self._llm is not None
        log.info("daily_report_job_started")

        try:
            report = await run_daily_report(self._llm)

            title = f"Iran OSINT Daily Report — {report.date}"

            for notifier in self._notifiers:
                try:
                    await notifier.send_report(title=title, markdown_body=report.markdown)
                except Exception as exc:
                    log.error(
                        "report_send_failed",
                        notifier=notifier.__class__.__name__,
                        error=str(exc),
                    )

            log.info(
                "daily_report_job_completed",
                date=report.date,
                tweet_count=report.total_count,
            )

        except Exception as exc:
            log.error("daily_report_job_failed", error=str(exc))

    async def shutdown(self) -> None:
        """全コンポーネントをクリーンアップする."""
        for collector in self._collectors:
            try:
                await collector.shutdown()
            except Exception as exc:
                log.error("collector_shutdown_error", error=str(exc))

        log.info("orchestrator_shutdown")
