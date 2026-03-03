"""SQLAlchemy ORM モデル定義.

収集ツイート、分析結果、デイリーレポートの3テーブルを定義する。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全モデルの基底クラス."""

    pass


class Tweet(Base):
    """収集されたツイート/ポストの永続化モデル.

    Attributes:
        id: 自動採番の主キー.
        tweet_id: Twitter/X 上のツイートID (重複排除に使用).
        author_handle: 投稿者のユーザーハンドル.
        author_name: 投稿者の表示名.
        text: ツイート本文 (原文).
        text_translated: 英語翻訳テキスト.
        lang: 検出言語コード (en, fa, ar, he, ur, fr 等).
        source: 収集ソース ("twikit", "tweepy", "rss").
        url: ツイートへの直リンク.
        retweet_count: リツイート数.
        like_count: いいね数.
        reply_count: リプライ数.
        posted_at: ツイートの投稿日時.
        collected_at: 収集日時.
    """

    __tablename__ = "tweets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    author_handle: Mapped[str] = mapped_column(String(255), nullable=False)
    author_name: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_translated: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str] = mapped_column(String(10), default="en")
    source: Mapped[str] = mapped_column(String(32), default="twikit")
    url: Mapped[str] = mapped_column(String(512), default="")
    retweet_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_tweets_posted_at", "posted_at"),
        Index("ix_tweets_lang", "lang"),
        Index("ix_tweets_source", "source"),
    )


class AnalysisResult(Base):
    """ツイートに対する分析結果.

    Attributes:
        id: 自動採番の主キー.
        tweet_id: 参照先ツイートID (tweets.tweet_id).
        topic: 分類されたトピック領域.
        sentiment_score: 感情スコア (-1.0 ~ 1.0).
        severity_score: 重大度スコア (0.0 ~ 1.0).
        is_breaking: 速報イベントとして検知されたか.
        summary: LLM による要約テキスト.
        keywords_matched: マッチしたキーワード群 (JSON文字列).
        analyzed_at: 分析実行日時.
    """

    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(32), nullable=False)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    severity_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_breaking: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_matched: Mapped[str] = mapped_column(Text, default="[]")
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_analysis_topic", "topic"),
        Index("ix_analysis_breaking", "is_breaking"),
        Index("ix_analysis_severity", "severity_score"),
    )


class Report(Base):
    """デイリーレポートの永続化モデル.

    Attributes:
        id: 自動採番の主キー.
        report_date: レポート対象日.
        report_type: レポート種別 ("daily", "breaking").
        content_markdown: Markdown 形式のレポート本文.
        content_html: HTML 形式のレポート本文.
        tweet_count: レポート対象ツイート総数.
        breaking_count: 速報イベント件数.
        sent_slack: Slack 送信済みフラグ.
        sent_email: Email 送信済みフラグ.
        created_at: レポート生成日時.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_date: Mapped[str] = mapped_column(String(10), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), default="daily")
    content_markdown: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    tweet_count: Mapped[int] = mapped_column(Integer, default=0)
    breaking_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_slack: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_email: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_reports_date_type", "report_date", "report_type"),
    )
