"""LLM 抽象化レイヤー.

Cloud (OpenAI / Anthropic) と Local (Ollama) の両方に対応した
統一インターフェースを提供する。settings.yaml の llm.provider で切替。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from utils.logger import get_logger
from utils.rate_limiter import llm_api_limiter

log = get_logger(__name__)


@dataclass
class LLMResponse:
    """LLM 応答の統一データ構造.

    Attributes:
        text: 応答テキスト.
        model: 使用モデル名.
        usage_prompt_tokens: 入力トークン数.
        usage_completion_tokens: 出力トークン数.
    """

    text: str
    model: str = ""
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0


class BaseLLMClient(ABC):
    """LLM クライアントの抽象基底クラス."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """テキスト補完を実行する.

        Args:
            system_prompt: システムプロンプト.
            user_prompt: ユーザープロンプト.
            temperature: 生成温度 (0.0 ~ 2.0).
            max_tokens: 最大出力トークン数.

        Returns:
            LLMResponse インスタンス.
        """
        ...

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict:
        """JSON 形式の応答を取得しパースする.

        Args:
            system_prompt: システムプロンプト (JSON出力を指示すること).
            user_prompt: ユーザープロンプト.
            temperature: 生成温度.
            max_tokens: 最大出力トークン数.

        Returns:
            パースされたJSON辞書. パース失敗時は {"raw": テキスト}.
        """
        resp = await self.complete(system_prompt, user_prompt, temperature, max_tokens)
        text = resp.text.strip()

        # コードブロックマーカーを除去
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("llm_json_parse_failed", raw_text=text[:200])
            return {"raw": text}


class OpenAIClient(BaseLLMClient):
    """OpenAI API (GPT-4o-mini 等) を使うクライアント.

    Attributes:
        _client: openai.AsyncOpenAI インスタンス.
        _model: 使用モデル名.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None) -> None:
        """コンストラクタ.

        Args:
            model: 使用するOpenAIモデル名.
            api_key: API キー. None なら環境変数 OPENAI_API_KEY を使用.
        """
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._model = model

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """OpenAI Chat Completions API を呼び出す.

        Args:
            system_prompt: システムプロンプト.
            user_prompt: ユーザープロンプト.
            temperature: 生成温度.
            max_tokens: 最大出力トークン数.

        Returns:
            LLMResponse インスタンス.

        Raises:
            openai.APIError: API 呼び出しに失敗した場合.
        """
        async with llm_api_limiter:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

        choice = response.choices[0]
        usage = response.usage

        return LLMResponse(
            text=choice.message.content or "",
            model=self._model,
            usage_prompt_tokens=usage.prompt_tokens if usage else 0,
            usage_completion_tokens=usage.completion_tokens if usage else 0,
        )


class AnthropicClient(BaseLLMClient):
    """Anthropic (Claude) API を使うクライアント.

    Attributes:
        _client: anthropic.AsyncAnthropic インスタンス.
        _model: 使用モデル名.
    """

    def __init__(
        self, model: str = "claude-sonnet-4-20250514", api_key: str | None = None
    ) -> None:
        """コンストラクタ.

        Args:
            model: 使用するClaudeモデル名.
            api_key: API キー. None なら環境変数 ANTHROPIC_API_KEY を使用.
        """
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self._model = model

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Anthropic Messages API を呼び出す.

        Args:
            system_prompt: システムプロンプト.
            user_prompt: ユーザープロンプト.
            temperature: 生成温度.
            max_tokens: 最大出力トークン数.

        Returns:
            LLMResponse インスタンス.

        Raises:
            anthropic.APIError: API 呼び出しに失敗した場合.
        """
        async with llm_api_limiter:
            response = await self._client.messages.create(
                model=self._model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        return LLMResponse(
            text=text,
            model=self._model,
            usage_prompt_tokens=response.usage.input_tokens,
            usage_completion_tokens=response.usage.output_tokens,
        )


class OllamaClient(BaseLLMClient):
    """Ollama (ローカルLLM) を使うクライアント.

    Attributes:
        _base_url: Ollama サーバーURL.
        _model: 使用モデル名.
    """

    def __init__(
        self,
        model: str = "llama3.1:8b",
        base_url: str | None = None,
    ) -> None:
        """コンストラクタ.

        Args:
            model: Ollama モデル名.
            base_url: Ollama サーバーURL. None なら環境変数または localhost.
        """
        self._base_url = (
            base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        )
        self._model = model

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Ollama /api/chat エンドポイントを呼び出す.

        Args:
            system_prompt: システムプロンプト.
            user_prompt: ユーザープロンプト.
            temperature: 生成温度.
            max_tokens: 最大出力トークン数.

        Returns:
            LLMResponse インスタンス.

        Raises:
            aiohttp.ClientError: Ollama サーバーに接続できない場合.
        """
        import aiohttp

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                data = await resp.json()

        message = data.get("message", {})
        return LLMResponse(
            text=message.get("content", ""),
            model=self._model,
        )


def create_llm_client(
    provider: str = "openai",
    model: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """設定に基づいて適切な LLM クライアントを生成するファクトリ.

    Args:
        provider: LLMプロバイダ ("openai", "anthropic", "ollama").
        model: モデル名. None ならプロバイダごとのデフォルトを使用.
        **kwargs: プロバイダ固有の追加引数.

    Returns:
        BaseLLMClient の具象インスタンス.

    Raises:
        ValueError: 未知のプロバイダが指定された場合.
    """
    if provider == "openai":
        return OpenAIClient(model=model or "gpt-4o-mini", **kwargs)
    elif provider == "anthropic":
        return AnthropicClient(model=model or "claude-sonnet-4-20250514", **kwargs)
    elif provider == "ollama":
        return OllamaClient(model=model or "llama3.1:8b", **kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
