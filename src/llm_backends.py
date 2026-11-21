"""LLM バックエンドの統一呼び出し。

Anthropic（claude-sonnet-5-5 など）と OpenRouter（任意のモデル）を
同じインターフェースで呼べるようにする。
"""
import json
import os
import re
import time

import anthropic
import httpx


ANTHROPIC_DEFAULT_MODEL = os.getenv("ANTHROPIC_QUIZ_MODEL", "claude-sonnet-5-5")
OPENROUTER_DEFAULT_MODEL = os.getenv("OPENROUTER_QUIZ_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "300"))
_OPENROUTER_MAX_RETRIES = 10


def parse_json(raw: str) -> dict | list:
    """LLM の応答から JSON を取り出す。

    優先順位:
      1. ```json ... ``` フェンス
      2. テキスト全体を直接 json.loads
      3. テキストの中から最初のバランスの取れた `{...}` または `[...]` を抽出
    """
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    extracted = _extract_json_block(text)
    if extracted is not None:
        return json.loads(extracted)
    raise json.JSONDecodeError(
        f"Could not extract JSON. raw head: {raw[:200]!r}", raw, 0,
    )


def _extract_json_block(text: str) -> str | None:
    """テキストから最初のバランスの取れた JSON ブロックを抽出する。"""
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        if start == -1:
            continue
        depth = 0
        in_string = False
        i = start
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == '"':
                in_string = not in_string
            elif not in_string:
                if c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
            i += 1
    return None


def resolve_model(backend: str, model: str) -> str:
    if model:
        return model
    if backend == "openrouter":
        return OPENROUTER_DEFAULT_MODEL
    return ANTHROPIC_DEFAULT_MODEL


# ── Anthropic ───────────────────────────────────────────────────────────────

_anthropic_client: anthropic.Anthropic | None = None


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _call_anthropic(*, model: str, system: str, user: str,
                    max_tokens: int, thinking_budget: int | None) -> str:
    kwargs: dict = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if thinking_budget:
        # budget_tokens 形式は Opus 4.7 / Sonnet 5 以降で 400 になるため adaptive thinking + effort を使う。
        # thinking_budget なしでも Sonnet 5.5 / Opus 5.5 は既定で adaptive thinking が有効になる。
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "high"}
    response = _get_anthropic_client().messages.create(**kwargs)
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError("Anthropic response did not contain a text block")


# ── OpenRouter ──────────────────────────────────────────────────────────────

def _call_openrouter(*, model: str, system: str, user: str, max_tokens: int) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    with httpx.Client(timeout=OPENROUTER_TIMEOUT) as client:
        for attempt in range(_OPENROUTER_MAX_RETRIES + 1):
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            if r.status_code == 429:
                if attempt >= _OPENROUTER_MAX_RETRIES:
                    break
                retry_after = int(r.headers.get("Retry-After", 0)) or 60
                print(f"[WARN] OpenRouter 429 — {retry_after}s後にリトライ ({attempt + 1}/{_OPENROUTER_MAX_RETRIES})...", flush=True)
                time.sleep(retry_after)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:300]}")
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                if attempt >= _OPENROUTER_MAX_RETRIES:
                    raise RuntimeError(f"OpenRouter response has no choices: {r.text[:300]}")
                print(f"[WARN] OpenRouter 空応答、リトライ ({attempt + 1}/{_OPENROUTER_MAX_RETRIES})", flush=True)
                time.sleep(5)
                continue
            content = choices[0].get("message", {}).get("content")
            if not content:
                if attempt >= _OPENROUTER_MAX_RETRIES:
                    raise RuntimeError(f"OpenRouter response message has no content: {r.text[:300]}")
                print(f"[WARN] OpenRouter content 空、リトライ ({attempt + 1}/{_OPENROUTER_MAX_RETRIES})", flush=True)
                time.sleep(5)
                continue
            return content

    raise RuntimeError("OpenRouter: 規定回数のリトライ後も応答が得られませんでした")


# ── 統一エントリポイント ─────────────────────────────────────────────────────

def call_llm(
    *,
    backend: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    thinking_budget: int | None = None,
) -> str:
    """同期呼び出し。backend ごとにディスパッチする。

    Args:
        backend: "anthropic" | "openrouter"
        model: 空のとき backend のデフォルトを resolve_model() で決定
        thinking_budget: Anthropic のみ対応。OpenRouter では無視される
    """
    resolved = resolve_model(backend, model)
    if backend == "openrouter":
        return _call_openrouter(model=resolved, system=system, user=user, max_tokens=max_tokens)
    if backend == "anthropic":
        return _call_anthropic(
            model=resolved, system=system, user=user,
            max_tokens=max_tokens, thinking_budget=thinking_budget,
        )
    raise ValueError(f"unknown backend: {backend}")
