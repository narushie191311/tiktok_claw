#!/usr/bin/env python3
"""日本の TikTok トレンド & ショートドラマ収集スクリプト.

日本語ロケール (ja-JP / Asia/Tokyo / region=JP) で TikTok にアクセスし、
日本向けトレンド動画とショートドラマを収集してコンソールに表示する。

使用例:
    # 日本の一般トレンド
    python Iran_ocint/scripts/japan_trends.py --mode general

    # ショートドラマのみ
    python Iran_ocint/scripts/japan_trends.py --mode drama

    # 全部 (一般 + ドラマ + グローバルトレンド日本語フィルタ)
    python Iran_ocint/scripts/japan_trends.py --mode all

    # カスタムタグ指定
    python Iran_ocint/scripts/japan_trends.py --tags ショートドラマ 恋愛 バズり
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from Iran_ocint.collectors.base import CollectedPost
from Iran_ocint.collectors.tiktok_japan_collector import (
    JP_ENTERTAINMENT_TAGS,
    JP_GENERAL_TAGS,
    JP_SHORT_DRAMA_TAGS,
    TikTokJapanCollector,
)
from Iran_ocint.utils.logger import get_logger

log = get_logger("japan_trends")


def _ensure_display() -> "subprocess.Popen[bytes] | None":
    """DISPLAY がない場合に Xvfb を自動起動する.

    Returns:
        起動した Xvfb プロセス。既に DISPLAY 設定済みなら None。
    """
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None

    display = ":99"
    try:
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x800x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        os.environ["DISPLAY"] = display
        print(f"  🖥️  Xvfb 起動 (DISPLAY={display})")
        return proc
    except FileNotFoundError:
        print("  ⚠️  Xvfb なし。headless=True で試みます (sudo apt install xvfb 推奨)")
        return None


def _print_section(title: str, posts: list[CollectedPost], max_show: int = 30) -> None:
    """収集結果セクションを表示する.

    エンゲージメント降順でソートし、日本語投稿を優先表示する。

    Args:
        title: セクションタイトル.
        posts: CollectedPost のリスト.
        max_show: 最大表示件数.
    """
    if not posts:
        print(f"\n⚠️  {title}: 結果なし")
        return

    # 日本語投稿を優先、次にいいね数で降順ソート
    jp_posts = sorted(
        [p for p in posts if p.lang == "ja"], key=lambda p: p.like_count, reverse=True
    )
    other_posts = sorted(
        [p for p in posts if p.lang != "ja"], key=lambda p: p.like_count, reverse=True
    )
    sorted_posts = jp_posts + other_posts

    print(f"\n{'='*72}")
    print(f"  {title}")
    print(f"  合計 {len(sorted_posts)} 件 | 日本語 {len(jp_posts)} 件 | その他 {len(other_posts)} 件")
    print(f"{'='*72}")

    for i, post in enumerate(sorted_posts[:max_show], 1):
        posted = post.posted_at.strftime("%Y-%m-%d") if post.posted_at else "----"
        text_preview = (post.text or "(テキストなし)")[:80].replace("\n", " ")
        lang_flag = "🇯🇵" if post.lang == "ja" else "🌏"
        tag = post.source.split("#")[-1] if "#" in post.source else post.source

        print(
            f"[{i:>3}] {lang_flag} @{post.author_handle:<20} "
            f"❤️{post.like_count:>8,}  🔁{post.retweet_count:>6,}  💬{post.reply_count:>6,}"
        )
        print(f"       📅{posted}  🏷️ #{tag}")
        print(f"       {text_preview}")
        print(f"       🔗 {post.url}")
        if i < len(sorted_posts[:max_show]):
            print()


async def run(args: argparse.Namespace) -> None:
    """メイン収集ロジック.

    Args:
        args: コマンドライン引数.
    """
    xvfb_proc = _ensure_display()

    print(f"\n🇯🇵 日本 TikTok トレンド収集 — モード: {args.mode}")
    print(f"   ロケール: ja-JP | タイムゾーン: Asia/Tokyo | Region: JP")
    print()

    collector = TikTokJapanCollector(mode=args.mode)

    try:
        print("📡 セッション初期化中... (初回は10〜15秒かかります)")
        await collector.initialize()

        if not collector._initialized:
            print("\n❌ 初期化失敗。ログを確認してください。")
            return

        print("✅ セッション確立完了\n")

        all_posts: list[CollectedPost] = []

        if args.tags:
            # カスタムタグ指定
            print(f"🔍 カスタムタグ検索: {args.tags}")
            posts = await collector.collect(args.tags, max_results=args.max)
            _print_section(f"カスタムタグ検索結果", posts, max_show=args.show)
            all_posts.extend(posts)

        else:
            if args.mode in ("general", "all"):
                print("📊 一般トレンドタグを収集中...")
                posts = await collector.collect(JP_GENERAL_TAGS, max_results=args.max)
                _print_section("🇯🇵 日本 一般トレンド", posts, max_show=args.show)
                all_posts.extend(posts)

            if args.mode in ("drama", "all"):
                print("\n🎬 ショートドラマタグを収集中...")
                drama_posts = await collector.collect_short_dramas(max_per_tag=args.max)
                _print_section("🎬 日本 ショートドラマ", drama_posts, max_show=args.show)
                all_posts.extend(drama_posts)

            if args.mode in ("entertainment", "all"):
                print("\n🎵 エンタメタグを収集中...")
                ent_posts = await collector.collect(JP_ENTERTAINMENT_TAGS, max_results=args.max)
                _print_section("🎵 日本 エンタメ", ent_posts, max_show=args.show)
                all_posts.extend(ent_posts)

            if args.trending:
                print("\n📈 グローバルトレンド (日本語フィルタ) を収集中...")
                trending_posts = await collector.get_trending_videos(count=50)
                _print_section("📈 グローバルトレンド (日本語優先)", trending_posts, max_show=args.show)
                all_posts.extend(trending_posts)

    finally:
        await collector.shutdown()
        if xvfb_proc:
            xvfb_proc.terminate()

    # サマリー
    if all_posts:
        jp_count = sum(1 for p in all_posts if p.lang == "ja")
        print(f"\n{'='*72}")
        print(f"✅ 収集完了: 合計 {len(all_posts)} 件 (日本語 {jp_count} 件)")

        top5 = sorted(all_posts, key=lambda p: p.like_count, reverse=True)[:5]
        print("\n🏆 総合トップ5:")
        for i, p in enumerate(top5, 1):
            flag = "🇯🇵" if p.lang == "ja" else "🌏"
            tag = p.source.split("#")[-1] if "#" in p.source else p.source
            print(f"  {i}. {flag} @{p.author_handle} | ❤️{p.like_count:,} | #{tag}")
            print(f"     {p.url}")
    else:
        print("\n❌ データを取得できませんでした。")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="日本の TikTok トレンド & ショートドラマ収集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["general", "drama", "entertainment", "all"],
        default="all",
        help="収集モード (default: all)",
    )
    parser.add_argument(
        "--tags",
        nargs="+",
        metavar="タグ",
        help="カスタムハッシュタグ (# 不要)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=15,
        metavar="N",
        help="タグあたり最大取得件数 (default: 15)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=20,
        metavar="N",
        help="表示件数上限 (default: 20)",
    )
    parser.add_argument(
        "--trending",
        action="store_true",
        help="グローバルトレンドも取得する",
    )

    parsed = parser.parse_args()
    asyncio.run(run(parsed))
