"""データコレクター抽象基底クラス.

全コレクター (Twitter API, Twitter Scraper, RSS) が実装すべき
共通インターフェースを定義する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CollectedPost:
    """収集されたポスト/記事の統一データ構造.

    全コレクターはこの形式でデータを返す。

    Attributes:
        post_id: プラットフォーム上の一意ID.
        author_handle: 投稿者ハンドル.
        author_name: 投稿者表示名.
        text: 本文テキスト.
        lang: 検出言語コード.
        url: 元投稿への直リンク.
        source: 収集元 ("twikit", "tweepy", "rss").
        retweet_count: リツイート/シェア数.
        like_count: いいね数.
        reply_count: リプライ数.
        posted_at: 投稿日時.
        raw_data: プラットフォーム固有の生データ.
    """

    post_id: str
    author_handle: str
    author_name: str
    text: str
    lang: str = "en"
    url: str = ""
    source: str = ""
    retweet_count: int = 0
    like_count: int = 0
    reply_count: int = 0
    posted_at: datetime | None = None
    raw_data: dict = field(default_factory=dict)

    def to_db_dict(self) -> dict:
        """Tweet テーブル挿入用の辞書に変換する.

        Returns:
            Tweet モデルのカラムに対応する辞書.
        """
        return {
            "tweet_id": self.post_id,
            "author_handle": self.author_handle,
            "author_name": self.author_name,
            "text": self.text,
            "lang": self.lang,
            "url": self.url,
            "source": self.source,
            "retweet_count": self.retweet_count,
            "like_count": self.like_count,
            "reply_count": self.reply_count,
            "posted_at": self.posted_at,
        }


class AbstractCollector(ABC):
    """データコレクターの抽象インターフェース.

    各コレクター実装は initialize() / collect() / shutdown() を提供する。
    """

    @abstractmethod
    async def initialize(self) -> None:
        """コレクターの初期化 (認証、セッション確立など).

        Raises:
            ConnectionError: 認証や接続に失敗した場合.
        """
        ...

    @abstractmethod
    async def collect(self, queries: list[str], max_results: int = 100) -> list[CollectedPost]:
        """指定クエリでデータを収集する.

        Args:
            queries: 検索クエリ文字列のリスト.
            max_results: クエリあたりの最大取得件数.

        Returns:
            CollectedPost のリスト.
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """コレクターのクリーンアップ (セッション閉じなど)."""
        ...
