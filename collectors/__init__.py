"""Iran_ocint.collectors — データ収集モジュール群.

利用可能なコレクター:
    - TwitterScraperCollector: twikit (Cookie 認証) による Twitter/X スクレイピング
    - TwitterAPICollector: tweepy (公式 API v2) による Twitter 収集
    - RSSCollector: feedparser による RSS/Atom フィード収集
    - TikTokCollector: TikTok-Api (Playwright) による TikTok トレンド収集
    - InstagramCollector: instagrapi (非公式 API) による Instagram ハッシュタグ収集
"""

from collectors.base import AbstractCollector, CollectedPost
from collectors.instagram_collector import InstagramCollector
from collectors.rss_collector import RSSCollector
from collectors.tiktok_collector import TikTokCollector
from collectors.twitter_api import TwitterAPICollector
from collectors.twitter_scraper import TwitterScraperCollector

__all__ = [
    "AbstractCollector",
    "CollectedPost",
    "TwitterScraperCollector",
    "TwitterAPICollector",
    "RSSCollector",
    "TikTokCollector",
    "InstagramCollector",
]
