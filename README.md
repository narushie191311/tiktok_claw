# Iran_ocint — Iran OSINT Monitoring & Analysis System

Twitter/X を中心にイランに関する公開情報（OSINT）を多言語で常時監視し、
LLM で分析・分類した結果を Slack / メールで自動配信するシステム。

## 機能

- **多言語監視**: 英語・ペルシャ語・アラビア語・ヘブライ語・ウルドゥー語・フランス語ほか
- **5 領域分類**: 地政学 / 軍事 / 経済 / 国内情勢 / サイバーセキュリティ
- **リアルタイムアラート**: 頻度スパイク + LLM トリアージで緊急イベントを即時通知
- **デイリーレポート**: 毎朝 09:00 JST に構造化レポートを Slack / メールへ配信
- **LLM 切替対応**: OpenAI / Anthropic (クラウド) と Ollama (ローカル) を設定で切替

## セットアップ

```bash
cd Iran_ocint
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env を編集して API キーなどを設定
```

## 起動

```bash
python main.py
```

### CLI オプション

| オプション | 説明 |
|---|---|
| `--once` | 1回だけ収集・分析を実行して終了 |
| `--report` | デイリーレポートを即時生成・送信 |
| `--dry-run` | 通知を送信せず分析結果をログに出力 |

## 設定ファイル

| ファイル | 用途 |
|---|---|
| `config/settings.yaml` | DB、LLM、通知、スケジューリング等の設定 |
| `config/keywords.yaml` | 領域別×言語別キーワード辞書 |
| `config/accounts.yaml` | 監視対象アカウント + RSS フィード一覧 |
| `.env` | APIキー、認証情報 (git 管理外) |

## アーキテクチャ

```
Collectors (Twitter/RSS) → Translator → Classifier → Event Detector → Summarizer
                                                           ↓                ↓
                                                      [即時通知]      [デイリーレポート]
                                                      Slack/Email      Slack/Email
```

## ライセンス

Private — Internal use only.
