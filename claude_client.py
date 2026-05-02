import json
import anthropic
from prompts import build_game_master_prompt, build_progress_prompt, build_reveal_prompt

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def ask_question(question: str, answer: str, user_message: str, history: list[dict] | None = None) -> str:
    messages = (history or []) + [{"role": "user", "content": user_message}]
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=build_game_master_prompt(question, answer),
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
    data = json.loads(raw)
    return data.get("covered", [])


def check_score(question: str, answer: str, user_theory: str, elements: list[dict]) -> tuple[int, list[str]]:
    covered = _evaluate_elements(question, answer, user_theory, elements)
    total = len(elements)
    progress = round(len(covered) / total * 100) if total > 0 else 0
    return progress, covered


def reveal_answer(question: str, answer: str, elements: list[dict]) -> str:
    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=build_reveal_prompt(question, answer, elements),
        messages=[{"role": "user", "content": "要点を箇条書きで述べてください"}],
    )
    return response.content[0].text.strip()
