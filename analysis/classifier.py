"""トピック分類 + 感情分析モジュール.

LLM を使ってテキストを5領域 (地政学/軍事/経済/国内/サイバー) に分類し、
感情スコアと重大度スコアを付与する。
"""

from __future__ import annotations

from dataclasses import dataclass

from Iran_ocint.analysis.llm_client import BaseLLMClient
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

VALID_TOPICS = {"geopolitics", "military", "economy", "domestic", "cyber", "other"}

CLASSIFICATION_SYSTEM_PROMPT = """You are an intelligence analyst specializing in Iran.
Analyze the given text and classify it. Return a JSON object with these fields:

{
  "topic": one of "geopolitics", "military", "economy", "domestic", "cyber", "other",
  "sentiment_score": float from -1.0 (very negative) to 1.0 (very positive),
  "severity_score": float from 0.0 (mundane) to 1.0 (critical/breaking),
  "is_breaking": boolean — true if this appears to be a breaking/urgent event,
  "summary": a concise 1-2 sentence summary in English,
  "key_entities": list of key named entities mentioned (people, organizations, places)
}

Topic definitions:
- geopolitics: diplomacy, sanctions, nuclear deal (JCPOA), international relations
- military: IRGC, missiles, proxy forces, military operations, defense
- economy: oil, currency (rial), trade, sanctions impact, energy
- domestic: protests, elections, human rights, governance, civil society
- cyber: cyber attacks, information warfare, digital surveillance, hacking

Return ONLY the JSON object, no additional text."""


@dataclass
class ClassificationResult:
    """分類結果のデータ構造.

    Attributes:
        topic: 分類されたトピック領域.
        sentiment_score: 感情スコア (-1.0 ~ 1.0).
        severity_score: 重大度スコア (0.0 ~ 1.0).
        is_breaking: 速報イベントか否か.
        summary: 英語の要約テキスト.
        key_entities: 検出された主要エンティティ.
    """

    topic: str
    sentiment_score: float
    severity_score: float
    is_breaking: bool
    summary: str
    key_entities: list[str]


async def classify_text(llm: BaseLLMClient, text: str) -> ClassificationResult:
    """テキストをトピック分類し、スコアリングする.

    Args:
        llm: LLM クライアントインスタンス.
        text: 分類対象テキスト (翻訳済みが望ましい).

    Returns:
        ClassificationResult インスタンス.
    """
    if not text or not text.strip():
        return _empty_result()

    try:
        data = await llm.complete_json(
            system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
            user_prompt=f"Analyze this text:\n\n{text}",
            temperature=0.1,
        )

        topic = data.get("topic", "other")
        if topic not in VALID_TOPICS:
            topic = "other"

        result = ClassificationResult(
            topic=topic,
            sentiment_score=_clamp(float(data.get("sentiment_score", 0.0)), -1.0, 1.0),
            severity_score=_clamp(float(data.get("severity_score", 0.0)), 0.0, 1.0),
            is_breaking=bool(data.get("is_breaking", False)),
            summary=data.get("summary", ""),
            key_entities=data.get("key_entities", []),
        )

        log.debug(
            "classification_done",
            topic=result.topic,
            severity=result.severity_score,
            breaking=result.is_breaking,
        )
        return result

    except Exception as exc:
        log.error("classification_failed", error=str(exc), text_preview=text[:100])
        return _empty_result()


async def classify_batch(
    llm: BaseLLMClient, texts: list[str]
) -> list[ClassificationResult]:
    """複数テキストを一括分類する.

    Args:
        llm: LLM クライアントインスタンス.
        texts: 分類対象テキストのリスト.

    Returns:
        ClassificationResult のリスト (入力と同じ順序).
    """
    results: list[ClassificationResult] = []
    for text in texts:
        result = await classify_text(llm, text)
        results.append(result)
    return results


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """値を指定範囲にクランプする.

    Args:
        value: 入力値.
        min_val: 最小値.
        max_val: 最大値.

    Returns:
        クランプされた値.
    """
    return max(min_val, min(max_val, value))


def _empty_result() -> ClassificationResult:
    """空の分類結果を返す.

    Returns:
        デフォルト値で初期化された ClassificationResult.
    """
    return ClassificationResult(
        topic="other",
        sentiment_score=0.0,
        severity_score=0.0,
        is_breaking=False,
        summary="",
        key_entities=[],
    )
