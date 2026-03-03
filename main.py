"""Iran_ocint — Iran OSINT Monitoring & Analysis System.

エントリポイント。CLI オプションに応じて常駐監視モード、
単発実行モード、レポート即時生成モードを切り替える。

使用方法:
    python main.py              # 常駐監視モード (15分間隔 + デイリーレポート)
    python main.py --once       # 1回だけ収集・分析
    python main.py --report     # デイリーレポート即時生成
    python main.py --dry-run    # 通知送信なし (ログ出力のみ)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from scheduler.jobs import OcintOrchestrator
from storage.database import close_db, init_db
from utils.logger import get_logger, setup_logging

log = get_logger(__name__)

# グレースフルシャットダウン用フラグ
_shutdown_requested = False


def parse_args() -> argparse.Namespace:
    """CLI 引数をパースする.

    Returns:
        パース済み引数の Namespace.
    """
    parser = argparse.ArgumentParser(
        prog="Iran_ocint",
        description="Iran OSINT Monitoring & Analysis System",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1回だけ収集・分析を実行して終了",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="デイリーレポートを即時生成・送信",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="通知を送信せず分析結果をログに出力",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="Iran_ocint/config/settings.yaml",
        help="設定ファイルパス (デフォルト: Iran_ocint/config/settings.yaml)",
    )
    return parser.parse_args()


def load_settings(config_path: str) -> dict:
    """設定ファイルを読み込む.

    Args:
        config_path: YAML 設定ファイルパス.

    Returns:
        設定辞書.
    """
    path = Path(config_path)
    if not path.exists():
        log.warning("config_not_found", path=config_path, msg="Using defaults")
        return {}

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def run_daemon(orchestrator: OcintOrchestrator, settings: dict) -> None:
    """常駐監視デーモンを実行する.

    APScheduler の代わりに asyncio.sleep ベースのシンプルなループで
    収集サイクルとデイリーレポートを実行する。

    Args:
        orchestrator: 初期化済みオーケストレーター.
        settings: アプリケーション設定辞書.
    """
    from datetime import datetime, time
    import zoneinfo

    interval_minutes = settings.get("collector", {}).get("collect_interval_minutes", 15)
    report_time_str = settings.get("daily_report", {}).get("time", "09:00")
    tz_name = settings.get("app", {}).get("timezone", "Asia/Tokyo")

    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        log.warning("timezone_fallback", tz=tz_name, fallback="UTC")
        tz = zoneinfo.ZoneInfo("UTC")

    report_hour, report_minute = map(int, report_time_str.split(":"))
    last_report_date: str | None = None

    log.info(
        "daemon_started",
        interval_minutes=interval_minutes,
        report_time=report_time_str,
        timezone=tz_name,
    )

    print(f"\n{'='*60}")
    print(f"  Iran_ocint — Iran OSINT Monitor")
    print(f"  Collection interval: {interval_minutes} min")
    print(f"  Daily report at: {report_time_str} {tz_name}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    while not _shutdown_requested:
        try:
            # 収集サイクル実行
            cycle_start = datetime.now(tz)
            print(f"[{cycle_start.strftime('%H:%M:%S')}] Starting collection cycle...")
            await orchestrator.run_collection_cycle()
            print(f"[{datetime.now(tz).strftime('%H:%M:%S')}] Collection cycle completed.")

            # デイリーレポート判定
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")
            report_time = time(report_hour, report_minute)

            if now.time() >= report_time and last_report_date != today_str:
                print(f"[{now.strftime('%H:%M:%S')}] Generating daily report...")
                await orchestrator.run_daily_report_job()
                last_report_date = today_str
                print(f"[{datetime.now(tz).strftime('%H:%M:%S')}] Daily report sent.")

            # 次のサイクルまで待機
            print(f"[{now.strftime('%H:%M:%S')}] Next cycle in {interval_minutes} min...")
            await asyncio.sleep(interval_minutes * 60)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("daemon_cycle_error", error=str(exc))
            print(f"[ERROR] Cycle failed: {exc}")
            await asyncio.sleep(60)


async def async_main(args: argparse.Namespace) -> None:
    """非同期メインエントリポイント.

    Args:
        args: パース済みCLI引数.
    """
    # 環境変数読み込み
    env_path = Path("Iran_ocint/.env")
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    # 設定読み込み
    settings = load_settings(args.config)

    # ログ設定
    log_cfg = settings.get("logging", {})
    log_file = log_cfg.get("file")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    setup_logging(
        level=log_cfg.get("level", "INFO"),
        fmt=log_cfg.get("format", "console"),
        log_file=log_file,
    )

    log.info("iran_ocint_starting", mode="once" if args.once else "report" if args.report else "daemon")

    # DB 初期化
    db_url = settings.get("database", {}).get("url")
    await init_db(db_url)

    # オーケストレーター初期化
    orchestrator = OcintOrchestrator(settings)
    await orchestrator.initialize()

    try:
        if args.once:
            print("Running single collection cycle...")
            await orchestrator.run_collection_cycle()
            print("Done.")

        elif args.report:
            print("Generating daily report...")
            await orchestrator.run_daily_report_job()
            print("Done.")

        else:
            await run_daemon(orchestrator, settings)

    except KeyboardInterrupt:
        print("\nShutting down...")

    finally:
        await orchestrator.shutdown()
        await close_db()
        log.info("iran_ocint_stopped")


def _handle_signal(signum: int, frame) -> None:
    """シグナルハンドラ (SIGINT/SIGTERM).

    Args:
        signum: シグナル番号.
        frame: スタックフレーム.
    """
    global _shutdown_requested
    _shutdown_requested = True
    log.info("shutdown_signal_received", signal=signum)


def main() -> None:
    """エントリポイント."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
