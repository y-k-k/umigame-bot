"""ゲームプレイ中の Claude API 呼び出し（出題者役・採点・答え合わせ）。

作問・校閲・出題準備は backend 切替に対応するため quiz_llm.py に分離した。
"""
import json
import os
import re

import anthropic

from prompts import (
    build_game_master_prompt,
    build_progress_prompt,
    build_reveal_prompt,
)

_client: anthropic.Anthropic | None = None

# ゲーム中（出題者役・採点・答え合わせ）のモデル設定。
# Sonnet 5.5 以降は thinking が既定で有効。effort=low なら簡単な応答では thinking をほぼ省略する。
GAME_MODEL = os.getenv("ANTHROPIC_GAME_MODEL", "claude-sonnet-5-5")
GAME_EFFORT = os.getenv("ANTHROPIC_GAME_EFFORT", "low")
# thinking も max_tokens に含まれるため余裕を持たせる（応答の長さはプロンプトで制御している）
_GAME_MAX_TOKENS = 16000

REFUSAL_REPLY = "（この内容にはお答えできませんでした。言い方を変えてもう一度試してください）"


class ClaudeRefusal(RuntimeError):
    """モデルが安全上の理由で応答を拒否した（stop_reason == "refusal"）。"""


def _parse_json(raw: str) -> dict | list:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _create_text(system: str, messages: list[dict]) -> str:
    """ゲーム用の呼び出し。本文（text ブロック）だけを結合して返す。

    応答の先頭に thinking ブロックが来ることがあるため、位置ではなく type で読む。
    """
    response = get_client().messages.create(
        model=GAME_MODEL,
        max_tokens=_GAME_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": GAME_EFFORT},
        system=system,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise ClaudeRefusal(f"refused (category={category})")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        print(f"[WARN] max_tokens に到達しました (model={GAME_MODEL})", flush=True)
    if not text:
        raise RuntimeError(f"本文のない応答でした (stop_reason={response.stop_reason})")
    return text


def ask_question(
    question: str,
    answer: str,
    user_message: str,
    history: list[dict] | None = None,
    extra_knowledge: dict | None = None,
) -> str:
    messages = (history or []) + [{"role": "user", "content": user_message}]
    try:
        return _create_text(build_game_master_prompt(question, answer, extra_knowledge), messages)
    except ClaudeRefusal:
        return REFUSAL_REPLY


def _evaluate_elements(question: str, answer: str, user_theory: str, elements: list[dict]) -> list[str]:
    raw = _create_text(
        build_progress_prompt(question, answer, user_theory, elements),
        [{"role": "user", "content": "評価してください"}],
    )
    data = _parse_json(raw)
    return data.get("covered", [])


def check_score(question: str, answer: str, user_theory: str, elements: list[dict]) -> tuple[int, list[str]]:
    covered = _evaluate_elements(question, answer, user_theory, elements)
    total = len(elements)
    progress = round(len(covered) / total * 100) if total > 0 else 0
    return progress, covered


def reveal_answer(question: str, answer: str, elements: list[dict]) -> str:
    try:
        return _create_text(
            build_reveal_prompt(question, answer, elements),
            [{"role": "user", "content": "要点を箇条書きで述べてください"}],
        )
    except ClaudeRefusal:
        return REFUSAL_REPLY
