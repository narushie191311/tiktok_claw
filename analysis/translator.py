"""多言語翻訳モジュール.

LLM を使ってペルシャ語・アラビア語・ヘブライ語・ウルドゥー語・フランス語等
のテキストを英語（および日本語）に翻訳する。
"""

from __future__ import annotations

from Iran_ocint.analysis.llm_client import BaseLLMClient
from Iran_ocint.utils.logger import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """You are a professional translator specializing in Middle Eastern geopolitics, 
military affairs, and intelligence analysis. Translate the given text into English accurately, 
preserving all proper nouns, organization names, and technical terms.

Important rules:
- Preserve all proper nouns in their commonly used English forms (e.g., IRGC, Quds Force, JCPOA)
- Keep numbers, dates, and statistics exact
- If the text is already in English, return it unchanged
- For ambiguous terms, include the original term in parentheses
- Return ONLY the translated text, no explanations"""

# 英語は翻訳不要
SKIP_LANGS = {"en", "und"}


async def translate_text(
    llm: BaseLLMClient,
    text: str,
    source_lang: str = "auto",
    target_lang: str = "en",
) -> str:
    """テキストをターゲット言語に翻訳する.

    Args:
        llm: LLM クライアントインスタンス.
        text: 翻訳対象テキスト.
        source_lang: ソース言語コード ("auto" で自動検出).
        target_lang: ターゲット言語コード.

    Returns:
        翻訳されたテキスト. 翻訳不要な場合は原文をそのまま返す.
    """
    if not text or not text.strip():
        return text

    if source_lang in SKIP_LANGS and target_lang == "en":
        return text

    lang_hint = f" (source language: {source_lang})" if source_lang != "auto" else ""

    prompt = f"Translate the following text into {target_lang}{lang_hint}:\n\n{text}"

    try:
        resp = await llm.complete(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=2048,
        )
        translated = resp.text.strip()
        log.debug(
            "translation_done",
            source_lang=source_lang,
            target_lang=target_lang,
            chars_in=len(text),
            chars_out=len(translated),
        )
        return translated

    except Exception as exc:
        log.error("translation_failed", error=str(exc), text_preview=text[:100])
        return text


async def translate_batch(
    llm: BaseLLMClient,
    texts: list[tuple[str, str]],
    target_lang: str = "en",
) -> list[str]:
    """複数テキストを一括翻訳する.

    Args:
        llm: LLM クライアントインスタンス.
        texts: (テキスト, 言語コード) のタプルリスト.
        target_lang: ターゲット言語コード.

    Returns:
        翻訳されたテキストのリスト (入力と同じ順序).
    """
    results: list[str] = []
    for text, lang in texts:
        translated = await translate_text(llm, text, source_lang=lang, target_lang=target_lang)
        results.append(translated)
    return results
