"""Thin OpenAI-compatible chat client (OpenAI, OpenRouter, Ollama, ...)."""
from __future__ import annotations

import json
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLM(Protocol):
    def chat(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


class LLMError(RuntimeError):
    pass


class OpenAICompatLLM:
    def __init__(self, settings: Settings, timeout: float = 120.0) -> None:
        from openai import OpenAI
        self.model = settings.llm_model
        self.client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "not-needed",  # Ollama ignores the key
            timeout=timeout,
            max_retries=2,
        )

    def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            **kwargs,
        )
        return resp.choices[0].message.content or ""


def make_llm(settings: Settings) -> LLM | None:
    """None when no LLM is configured; callers degrade gracefully."""
    return OpenAICompatLLM(settings) if settings.llm_configured else None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_model(raw: str, model: type[T]) -> T:
    """Parse LLM output into a Pydantic model, tolerating code fences/preamble."""
    text = _FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return model.model_validate(json.loads(text[start:end + 1]))


def chat_json(llm: LLM, system: str, user: str, model: type[T]) -> T:
    """Ask for JSON and validate; retry once with the error fed back."""
    raw = llm.chat(system, user, json_mode=True)
    try:
        return parse_json_model(raw, model)
    except (ValueError, ValidationError) as first:
        retry_user = (
            f"{user}\n\nYour previous reply was invalid ({str(first)[:300]}). "
            "Reply again with ONLY a JSON object matching the schema."
        )
        raw = llm.chat(system, retry_user, json_mode=True)
        try:
            return parse_json_model(raw, model)
        except (ValueError, ValidationError) as second:
            raise LLMError(f"invalid JSON from LLM after retry: {second}") from second
