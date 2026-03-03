"""非同期データベースセッション管理モジュール.

SQLAlchemy async engine + session factory を提供し、
テーブルの自動作成と CRUD ヘルパーを含む。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from Iran_ocint.storage.models import AnalysisResult, Base, Report, Tweet
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(database_url: str | None = None) -> None:
    """データベースエンジンを初期化し、テーブルを作成する.

    Args:
        database_url: SQLAlchemy 非同期接続URL.
            None の場合は環境変数またはデフォルトの SQLite パスを使用.
    """
    global _engine, _session_factory

    if database_url is None:
        database_url = os.getenv(
            "DATABASE_URL",
            "sqlite+aiosqlite:///Iran_ocint/data/ocint.db",
        )

    db_path = database_url.replace("sqlite+aiosqlite:///", "")
    if db_path != database_url:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("database_initialized", url=database_url)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """非同期 DB セッションを取得するコンテキストマネージャ.

    Yields:
        AsyncSession インスタンス.

    Raises:
        RuntimeError: init_db() が呼ばれていない場合.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def upsert_tweet(session: AsyncSession, tweet_data: dict) -> Tweet | None:
    """ツイートを挿入 (既存なら無視).

    Args:
        session: 非同期DBセッション.
        tweet_data: Tweet モデルのフィールドを含む辞書.

    Returns:
        挿入された Tweet. 既存の場合は None.
    """
    existing = await session.execute(
        select(Tweet).where(Tweet.tweet_id == tweet_data["tweet_id"])
    )
    if existing.scalar_one_or_none() is not None:
        return None

    tweet = Tweet(**tweet_data)
    session.add(tweet)
    await session.flush()
    log.debug("tweet_stored", tweet_id=tweet_data["tweet_id"])
    return tweet


async def store_analysis(session: AsyncSession, result_data: dict) -> AnalysisResult:
    """分析結果を保存する.

    Args:
        session: 非同期DBセッション.
        result_data: AnalysisResult モデルのフィールドを含む辞書.

    Returns:
        保存された AnalysisResult インスタンス.
    """
    result = AnalysisResult(**result_data)
    session.add(result)
    await session.flush()
    return result


async def store_report(session: AsyncSession, report_data: dict) -> Report:
    """レポートを保存する.

    Args:
        session: 非同期DBセッション.
        report_data: Report モデルのフィールドを含む辞書.

    Returns:
        保存された Report インスタンス.
    """
    report = Report(**report_data)
    session.add(report)
    await session.flush()
    return report


async def get_tweets_since(
    session: AsyncSession,
    since: datetime,
    topic: str | None = None,
) -> list[Tweet]:
    """指定日時以降のツイートを取得する.

    Args:
        session: 非同期DBセッション.
        since: この日時以降のツイートを返す.
        topic: トピックでフィルタする場合に指定.

    Returns:
        Tweet のリスト (新しい順).
    """
    stmt = select(Tweet).where(Tweet.collected_at >= since).order_by(Tweet.posted_at.desc())

    if topic:
        stmt = stmt.join(
            AnalysisResult, AnalysisResult.tweet_id == Tweet.tweet_id
        ).where(AnalysisResult.topic == topic)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_tweet_count_in_window(
    session: AsyncSession,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """指定時間窓内のツイート件数を返す (スパイク検知用).

    Args:
        session: 非同期DBセッション.
        window_start: 窓の開始日時.
        window_end: 窓の終了日時.

    Returns:
        ツイート件数.
    """
    result = await session.execute(
        select(func.count(Tweet.id)).where(
            Tweet.collected_at >= window_start,
            Tweet.collected_at <= window_end,
        )
    )
    return result.scalar_one()


async def get_hourly_counts(session: AsyncSession, hours: int = 24) -> list[int]:
    """過去 N 時間の1時間ごとのツイート件数を返す.

    Args:
        session: 非同期DBセッション.
        hours: 遡る時間数.

    Returns:
        1時間ごとの件数リスト (古い順).
    """
    now = datetime.now(UTC)
    counts: list[int] = []
    for h in range(hours, 0, -1):
        window_start = now - timedelta(hours=h)
        window_end = now - timedelta(hours=h - 1)
        count = await get_tweet_count_in_window(session, window_start, window_end)
        counts.append(count)
    return counts


async def close_db() -> None:
    """データベースエンジンを閉じる."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        log.info("database_closed")
