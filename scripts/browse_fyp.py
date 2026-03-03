#!/usr/bin/env python3
"""TikTok FYP (おすすめ) を実際にブラウズしてトレンドを収集するスクリプト.

タグ検索ではなく、TikTok のアルゴリズムが表示する「おすすめ」を
Playwright で実際にスクロールして収集する。

各動画について:
  - 再生数 / いいね数 / コメント数 / シェア数 / 動画秒数
  - 投稿日時 (JST)
  - 最多いいねコメント
  - Gemini AI による映像分析 (映像説明・カテゴリ・バズ理由・タグ)
  - Slack Webhook 通知

使用例:
    # FYP をスクロールして上位10件を AI 分析 + Slack 通知
    python Iran_ocint/scripts/browse_fyp.py \\
        --scrolls 15 --analyze 10 \\
        --slack-webhook https://hooks.slack.com/services/...

    # 環境変数で設定
    export GEMINI_API_KEY="your-api-key"
    export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from Iran_ocint.collectors.tiktok_fyp_crawler import FYPVideo, TikTokFYPCrawler, VideoAnalyzer
from Iran_ocint.utils.logger import get_logger
from Iran_ocint.utils.slack_notifier import SlackNotifier

log = get_logger("browse_fyp")


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
        print("  ⚠️  Xvfb なし (sudo apt install xvfb 推奨)")
        return None


def _fmt_count(n: int) -> str:
    """数値を読みやすい形式にフォーマットする.

    Args:
        n: 数値.

    Returns:
        フォーマット済み文字列 (例: "220万", "93.1K", "5,234").
    """
    if n >= 100_000_000:
        return f"{n/100_000_000:.1f}億"
    if n >= 10_000:
        return f"{n/10_000:.1f}万"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


def _fmt_duration(sec: int) -> str:
    """秒数を mm:ss 形式に変換する.

    Args:
        sec: 秒数.

    Returns:
        "1:23" 形式の文字列、0秒の場合は空文字列.
    """
    if not sec:
        return ""
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"


def _print_video(v: FYPVideo, idx: int, ai_result: dict | None = None) -> None:
    """動画情報を表示する.

    Args:
        v: FYPVideo インスタンス.
        idx: 表示番号.
        ai_result: Gemini 分析結果辞書 (None の場合は表示しない).
    """
    # 説明文からハッシュタグを除去し、タグは別途表示
    raw_desc = v.description or "(テキストなし)"
    desc = re.sub(r"#\S+", "", raw_desc).strip()[:100].replace("\n", " ")
    # 実際のハッシュタグを抽出
    real_tags = re.findall(r"#(\S+)", v.description or "")
    dur = _fmt_duration(v.duration_sec)
    posted = v.posted_at_jst()

    print(f"\n[{idx:>3}] @{v.author_handle}")

    # 統計行
    stats = (
        f"       ❤️ {_fmt_count(v.like_count):>7}  "
        f"💬 {_fmt_count(v.comment_count):>6}  "
        f"🔁 {_fmt_count(v.share_count):>6}  "
        f"▶️  {_fmt_count(v.play_count):>8}"
    )
    if dur:
        stats += f"  ⏱ {dur}"
    if posted:
        stats += f"  📅 {posted}"
    print(stats)
    print(f"       {desc}")
    print(f"       🔗 {v.url}")

    # 最多いいねコメント
    if v.top_comment:
        c_text = v.top_comment.get("text", "")[:80].replace("\n", " ")
        c_likes = _fmt_count(v.top_comment.get("likes", 0))
        print(f"       「{c_text}」（❤️{c_likes}）")

    # AI 分析
    if ai_result and "error" not in ai_result:
        print(f"\n       AI分析:")
        desc_key = "visual_description" if "visual_description" in ai_result else "description"
        if ai_result.get(desc_key):
            print(f"         \033[1m説明\033[0m: {ai_result[desc_key][:160]}")
        print(f"         カテゴリ: {ai_result.get('category', '')}")
        print(f"         バズ理由: {ai_result.get('trend_reason', '')[:100]}")
        print(f"         感情: {ai_result.get('emotion', '')}")
        # タグは実際のハッシュタグを優先、なければ AI 提案
        display_tags = real_tags or ai_result.get("tags", [])
        if display_tags:
            print(f"         \033[1mタグ\033[0m: {' '.join(f'#{t}' for t in display_tags[:8])}")


async def run(args: argparse.Namespace) -> None:
    """メイン処理.

    Args:
        args: コマンドライン引数.
    """
    xvfb_proc = _ensure_display()

    # Slack 設定確認
    slack_url = args.slack_webhook or os.environ.get("SLACK_WEBHOOK_URL", "")
    slack_notifier = SlackNotifier(slack_url) if slack_url else None

    # AI 分析件数: 明示指定がなければ --slack-top に合わせる
    analyze_count = args.analyze if args.analyze >= 0 else args.slack_top

    print("\n🇯🇵 TikTok FYP ブラウザー")
    print(f"   スクロール回数: {args.scrolls}回 | 日本語設定: ON")
    if args.min_plays > 0:
        print(f"   再生数フィルター: {args.min_plays/10000:.0f}万再生以上のみ")
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    print(f"   AI分析: 上位{analyze_count}件 | Gemini: {'✅ キーあり' if has_key else '❌ GEMINI_API_KEY 未設定'}")
    print(f"   最多いいねコメント: ✅ 上位{args.slack_top}件")
    if slack_notifier:
        print(f"   Slack通知: ✅ 上位{args.slack_top}件を送信")
    else:
        print("   Slack通知: ❌ (--slack-webhook または SLACK_WEBHOOK_URL で設定)")

    analyzer = VideoAnalyzer()
    crawler = TikTokFYPCrawler()

    try:
        print("\n📡 ブラウザ起動中...")
        await crawler.initialize()
        print("✅ ブラウザ起動完了\n")

        print(f"🔄 TikTok おすすめフィードを {args.scrolls} 回スクロール中...")
        print("   (動画が表示されるまでお待ちください)\n")

        videos = await crawler.crawl_fyp(
            scroll_count=args.scrolls,
            language="ja",
            wait_between=args.wait,
        )

        if not videos:
            print("❌ 動画を取得できませんでした")
            sys.exit(1)

        # 再生数フィルター (ブラウザが開いている間に実行)
        if args.min_plays > 0:
            before = len(videos)
            videos = [v for v in videos if v.play_count >= args.min_plays]
            print(f"\n🔍 再生数フィルター: {args.min_plays/10000:.0f}万再生以上 → {before}件 → {len(videos)}件")
            if not videos:
                print("   該当動画なし (0件)")
                sys.exit(0)

        # 日本語優先でソート
        jp_videos = [v for v in videos if v.is_japanese()]
        other_videos = [v for v in videos if not v.is_japanese()]
        sorted_videos = jp_videos + other_videos

        # ブラウザを使って上位N件のコメントを取得 (ブラウザが開いている間に実行)
        top_n = args.slack_top
        comment_targets = sorted_videos[:top_n]
        if comment_targets:
            print(f"💬 最多いいねコメント取得中 (上位{len(comment_targets)}件)...")
            comment_map = await crawler.fetch_top_comments(
                [v.video_id for v in comment_targets],
                delay_sec=1.2,
            )
            for v in sorted_videos:
                if v.video_id in comment_map:
                    v.top_comment = comment_map[v.video_id]
            ok_count = sum(1 for v in comment_targets if v.top_comment)
            print(f"   ✅ {ok_count}/{len(comment_targets)} 件取得完了\n")

    finally:
        await crawler.shutdown()
        if xvfb_proc:
            xvfb_proc.terminate()

    print(f"\n{'='*72}")
    print(f"  🇯🇵 TikTok おすすめ収集結果")
    print(f"  合計 {len(sorted_videos)} 件 | 日本語 {len(jp_videos)} 件 | その他 {len(other_videos)} 件")
    print(f"{'='*72}")

    # AI 分析 (上位 N 件)
    ai_results: dict[str, dict] = {}
    if analyze_count > 0 and analyzer.is_configured():
        targets = sorted_videos[:analyze_count]
        print(f"\n🤖 Gemini AI 分析中 (上位 {len(targets)} 件)...")
        for i, v in enumerate(targets, 1):
            print(f"   [{i}/{len(targets)}] 分析中: @{v.author_handle} ({v.url[-38:]})")
            result = await analyzer.analyze_video(v.url, extra_context=v.description)
            ai_results[v.video_id] = result
            await asyncio.sleep(2)
        print()
    elif analyze_count > 0 and not analyzer.is_configured():
        print("\n⚠️  GEMINI_API_KEY が未設定のため AI 分析をスキップします")
        print("   設定: export GEMINI_API_KEY='your-api-key'")
        print("   取得: https://aistudio.google.com/app/apikey\n")

    # 表示
    show_count = min(len(sorted_videos), args.show)
    for i, v in enumerate(sorted_videos[:show_count], 1):
        _print_video(v, i, ai_results.get(v.video_id))

    # サマリー
    print(f"\n{'='*72}")
    print(f"✅ 合計 {len(sorted_videos)} 件 (日本語 {len(jp_videos)} 件)")

    if sorted_videos:
        top = sorted_videos[0]
        flag = "🇯🇵" if top.is_japanese() else "🌏"
        dur = _fmt_duration(top.duration_sec)
        extra = f" | ⏱ {dur}" if dur else ""
        extra += f" | 📅 {top.posted_at_jst()}" if top.posted_at_jst() else ""
        print(f"\n🏆 1位: {flag} @{top.author_handle}")
        print(f"   ❤️ {_fmt_count(top.like_count)} | ▶️ {_fmt_count(top.play_count)}{extra}")
        print(f"   {top.url}")

    # Slack 通知
    if slack_notifier:
        print(f"\n📨 Slack に上位 {top_n} 件を送信中...")
        ok = await slack_notifier.send_trend_report(
            videos=sorted_videos,
            ai_results=ai_results,
            top_n=top_n,
        )
        if ok:
            print("   ✅ Slack 送信完了")
        else:
            print("   ❌ Slack 送信失敗 (ログを確認してください)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TikTok FYP をブラウズしてトレンドを収集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=10,
        metavar="N",
        help="スクロール回数 (1回で3〜5件取得目安, default: 10)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=2.5,
        metavar="SEC",
        help="スクロール間の待機秒数 (default: 2.5)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=30,
        metavar="N",
        help="表示件数上限 (default: 30)",
    )
    parser.add_argument(
        "--analyze",
        type=int,
        default=-1,
        metavar="N",
        help="Gemini AI 分析件数 (default: --slack-top と同数)",
    )
    parser.add_argument(
        "--slack-webhook",
        type=str,
        default="",
        metavar="URL",
        help="Slack Incoming Webhook URL (省略時は SLACK_WEBHOOK_URL 環境変数を使用)",
    )
    parser.add_argument(
        "--slack-top",
        type=int,
        default=10,
        metavar="N",
        help="Slack に送信する上位件数 / コメント・AI分析の対象件数 (default: 10)",
    )
    parser.add_argument(
        "--min-plays",
        type=int,
        default=1_000_000,
        metavar="N",
        help="最低再生数フィルター (default: 1000000 = 100万再生以上のみ対象, 0=フィルタなし)",
    )

    parsed = parser.parse_args()
    asyncio.run(run(parsed))
