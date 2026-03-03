"""API 呼び出しレート制御ユーティリティ.

aiolimiter を使った非同期レートリミッターを提供する。
"""

from __future__ import annotations

from aiolimiter import AsyncLimiter


def create_limiter(max_rate: float, time_period: float = 60.0) -> AsyncLimiter:
    """非同期レートリミッターを生成する.

    Args:
        max_rate: time_period 内の最大リクエスト数.
        time_period: 制限期間 (秒). デフォルト 60 秒.

    Returns:
        AsyncLimiter インスタンス. async with limiter: で使用.

    Example:
        >>> limiter = create_limiter(30, 60.0)  # 60秒に30リクエスト
        >>> async with limiter:
        ...     await make_api_call()
    """
    return AsyncLimiter(max_rate, time_period)


# Twitter API v2 Basic: 10,000 tweets/month ≒ ~14/hour (余裕を持って10/min)
twitter_api_limiter = create_limiter(max_rate=10, time_period=60.0)

# twikit scraping: 控えめに 20 req/min
twitter_scraper_limiter = create_limiter(max_rate=20, time_period=60.0)

# LLM API: OpenAI GPT-4o-mini は余裕があるが念のため 30 req/min
llm_api_limiter = create_limiter(max_rate=30, time_period=60.0)

# RSS: 各フィード 1 req/5min
rss_limiter = create_limiter(max_rate=12, time_period=60.0)

# TikTok: ブロック回避のため控えめに 10 req/min
tiktok_limiter = create_limiter(max_rate=10, time_period=60.0)

# Instagram: instagrapi 推奨レートに合わせて 6 req/min
instagram_limiter = create_limiter(max_rate=6, time_period=60.0)
