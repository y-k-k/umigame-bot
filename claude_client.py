import json
import re
import anthropic
from prompts import (
    build_game_master_prompt,
    build_progress_prompt,
    build_reveal_prompt,
    build_quiz_generator_prompt,
    build_quiz_reviewer_prompt,
    build_quiz_preparer_prompt,
)

_client = None


def _parse_json(raw: str) -> dict | list:
    text = raw.strip()
    # ```json ... ``` または ``` ... ``` を剥がす
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def ask_question(
    question: str,
    answer: str,
    user_message: str,
    history: list[dict] | None = None,
    extra_knowledge: dict | None = None,
) -> str:
    messages = (history or []) + [{"role": "user", "content": user_message}]
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=build_game_master_prompt(question, answer, extra_knowledge),
        messages=messages,
    )
    return response.content[0].text.strip()


def _evaluate_elements(question: str, answer: str, user_theory: str, elements: list[dict]) -> list[str]:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        system=build_progress_prompt(question, answer, user_theory, elements),
        messages=[{"role": "user", "content": "評価してください"}],
    )
    raw = response.content[0].text.strip()
    data = _parse_json(raw)
    return data.get("covered", [])


def check_score(question: str, answer: str, user_theory: str, elements: list[dict]) -> tuple[int, list[str]]:
    covered = _evaluate_elements(question, answer, user_theory, elements)
    total = len(elements)
    progress = round(len(covered) / total * 100) if total > 0 else 0
    return progress, covered


def generate_quiz(puzzles: list[dict], previous_feedback: str | None = None) -> dict:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=12000,
        thinking={"type": "enabled", "budget_tokens": 10000},
        system=build_quiz_generator_prompt(puzzles, previous_feedback),
        messages=[{"role": "user", "content": "新しい問題を1つ作成してください"}],
    )
    for block in response.content:
        if block.type == "text":
            return _parse_json(block.text)
    raise ValueError("text block not found in response")


def review_quiz(question: str, answer: str) -> dict:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=build_quiz_reviewer_prompt(),
        messages=[{"role": "user", "content": f"## 問題\n{question}\n\n## 答え\n{answer}"}],
    )
    return _parse_json(response.content[0].text)


def prepare_quiz(question: str, answer: str) -> dict:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=build_quiz_preparer_prompt(),
        messages=[{"role": "user", "content": f"## 問題\n{question}\n\n## 答え\n{answer}"}],
    )
    return _parse_json(response.content[0].text)


def reveal_answer(question: str, answer: str, elements: list[dict]) -> str:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=build_reveal_prompt(question, answer, elements),
        messages=[{"role": "user", "content": "要点を箇条書きで述べてください"}],
    )
    return response.content[0].text.strip()
