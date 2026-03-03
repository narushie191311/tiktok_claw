#!/usr/bin/env python3
"""ソーシャルメディア トレンド手動チェックスクリプト.

TikTok または Instagram から指定ハッシュタグのトレンドを手動収集し、
結果をコンソールに出力する。設定確認・初期テスト用。

使用例:
    # TikTok の #viral #fyp トレンドを取得
    python Iran_ocint/scripts/check_social_trends.py --platform tiktok --tags viral fyp ai

    # Instagram の #tech #ai トレンドを取得
    python Iran_ocint/scripts/check_social_trends.py --platform instagram --tags tech ai startup

    # 両方 + グローバルトレンド
    python Iran_ocint/scripts/check_social_trends.py --platform all --tags trending viral
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

# リポジトリルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from Iran_ocint.collectors.base import CollectedPost
from Iran_ocint.utils.logger import get_logger


def _ensure_display() -> "subprocess.Popen[bytes] | None":
    """DISPLAY 環境変数がない場合に Xvfb を自動起動する.

    TikTok のハッシュタグ API はヘッドレス Chromium をブロックするため、
    仮想ディスプレイ経由で headless=False にする必要がある。

    Returns:
        起動した Xvfb プロセス。既に DISPLAY が設定済みなら None。
    """
    import subprocess
    import time

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
        print(f"  🖥️  Xvfb 仮想ディスプレイを起動しました (DISPLAY={display})")
        return proc
    except FileNotFoundError:
        print("  ⚠️  Xvfb が見つかりません。headless=True で試みます。")
        print("     インストール: sudo apt install xvfb")
        return None

log = get_logger("check_social_trends")


def _fmt_post(post: CollectedPost, idx: int) -> str:
    """収集ポストを見やすい形式にフォーマットする.

    Args:
        post: CollectedPost インスタンス.
        idx: 表示番号.

    Returns:
        フォーマット済み文字列.
    """
    posted = post.posted_at.strftime("%Y-%m-%d %H:%M") if post.posted_at else "N/A"
    text_preview = post.text[:120].replace("\n", " ") if post.text else "(no text)"
    return (
        f"[{idx:>3}] @{post.author_handle} | ❤️ {post.like_count:>7,} | "
        f"🔁 {post.retweet_count:>5,} | 💬 {post.reply_count:>5,}\n"
        f"      {posted} | {post.url}\n"
        f"      {text_preview}"
    )


async def collect_tiktok(tags: list[str], max_per_tag: int) -> list[CollectedPost]:
    """TikTok からハッシュタグ別トレンドを収集する.

    Args:
        tags: ハッシュタグリスト.
        max_per_tag: タグあたり最大取得件数.

    Returns:
        CollectedPost のリスト.
    """
    try:
        from Iran_ocint.collectors.tiktok_collector import TikTokCollector
    except ImportError as e:
        print(f"[ERROR] TikTokCollector をインポートできません: {e}")
        return []

    collector = TikTokCollector()
    try:
        await collector.initialize()
    except ImportError as e:
        print(f"[ERROR] TikTok 依存関係が不足しています: {e}")
        print("  → pip install TikTokApi && playwright install chromium")
        return []

    print(f"\n🎵 TikTok — {len(tags)} タグを収集中...")
    posts = await collector.collect(tags, max_results=max_per_tag)

    # グローバルトレンドも取得
    print("  📈 グローバルトレンドも取得中...")
    trending = await collector.get_trending_videos(count=20)
    posts.extend(trending)

    await collector.shutdown()
    return posts


async def collect_instagram(tags: list[str], max_per_tag: int) -> list[CollectedPost]:
    """Instagram からハッシュタグ別投稿を収集する.

    Args:
        tags: ハッシュタグリスト.
        max_per_tag: タグあたり最大取得件数.

    Returns:
        CollectedPost のリスト.
    """
    try:
        from Iran_ocint.collectors.instagram_collector import InstagramCollector
    except ImportError as e:
        print(f"[ERROR] InstagramCollector をインポートできません: {e}")
        return []

    collector = InstagramCollector()
    try:
        await collector.initialize()
    except ImportError as e:
        print(f"[ERROR] Instagram 依存関係が不足しています: {e}")
        print("  → pip install instagrapi")
        return []

    print(f"\n📷 Instagram — {len(tags)} タグを収集中...")
    posts = await collector.collect(tags, max_results=max_per_tag)
    await collector.shutdown()
    return posts


def _print_results(posts: list[CollectedPost], platform_label: str) -> None:
    """収集結果をコンソールに表示する.

    エンゲージメント (like_count) 降順でソートして表示する。

    Args:
        posts: CollectedPost のリスト.
        platform_label: 表示用プラットフォーム名.
    """
    if not posts:
        print(f"\n⚠️  {platform_label}: 結果が空でした (認証確認/ネット接続を確認)")
        return

    sorted_posts = sorted(posts, key=lambda p: p.like_count, reverse=True)

    print(f"\n{'='*70}")
    print(f"  {platform_label} — 上位 {len(sorted_posts)} 件 (エンゲージメント降順)")
    print(f"{'='*70}")

    for i, post in enumerate(sorted_posts[:50], 1):
        print(_fmt_post(post, i))
        if i < len(sorted_posts):
            print()


async def main(args: argparse.Namespace) -> None:
    """メインエントリーポイント.

    Args:
        args: コマンドライン引数.
    """
    tags = args.tags or ["viral", "trending", "fyp"]
    max_per_tag = args.max

    # TikTok 使用時は仮想ディスプレイを確保 (ハッシュタグ bot 検知回避)
    xvfb_proc = None
    if args.platform in ("tiktok", "all"):
        xvfb_proc = _ensure_display()

    all_posts: list[CollectedPost] = []

    if args.platform in ("tiktok", "all"):
        tiktok_posts = await collect_tiktok(tags, max_per_tag)
        _print_results(tiktok_posts, "TikTok")
        all_posts.extend(tiktok_posts)

    if args.platform in ("instagram", "all"):
        ig_posts = await collect_instagram(tags, max_per_tag)
        _print_results(ig_posts, "Instagram")
        all_posts.extend(ig_posts)

    # Xvfb を起動していた場合はクリーンアップ
    if xvfb_proc is not None:
        xvfb_proc.terminate()

    if all_posts:
        print(f"\n✅ 合計 {len(all_posts)} 件収集完了")
        top3 = sorted(all_posts, key=lambda p: p.like_count, reverse=True)[:3]
        print("\n🏆 総合トップ3:")
        for i, p in enumerate(top3, 1):
            print(f"  {i}. [{p.source}] @{p.author_handle} — ❤️{p.like_count:,}")
            print(f"     {p.url}")
    else:
        print("\n❌ データを収集できませんでした。")
        print("   - 認証情報 (.env) を確認してください")
        print("   - 依存パッケージのインストールを確認してください")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TikTok / Instagram トレンドを手動収集するスクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--platform",
        choices=["tiktok", "instagram", "all"],
        default="tiktok",
        help="収集対象プラットフォーム (default: tiktok)",
    )
    parser.add_argument(
        "--tags",
        nargs="+",
        metavar="TAG",
        help="収集するハッシュタグ (# 不要). 例: viral fyp ai",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=20,
        metavar="N",
        help="タグあたり最大取得件数 (default: 20)",
    )

    parsed = parser.parse_args()
    asyncio.run(main(parsed))
