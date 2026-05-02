import json
from pathlib import Path
from dataclasses import dataclass, field


PUZZLES_PATH = Path(__file__).parent / "puzzles" / "puzzles.json"


@dataclass
class GameSession:
    puzzle_id: int
    question: str
    answer: str
    question_count: int = 0
    elements: list[dict] = field(default_factory=list)
    revealed_ids: set[str] = field(default_factory=set)


_sessions: dict[int, GameSession] = {}
_puzzles: list[dict] | None = None


def _load_puzzles() -> list[dict]:
    global _puzzles
    if _puzzles is None:
        with open(PUZZLES_PATH, encoding="utf-8") as f:
            _puzzles = json.load(f)
    return _puzzles


def list_puzzles() -> list[dict]:
    return [{"id": p["id"], "title": p["title"]} for p in _load_puzzles()]


def get_puzzle(puzzle_id: int) -> dict | None:
    return next((p for p in _load_puzzles() if p["id"] == puzzle_id), None)


def start_session(channel_id: int, puzzle_id: int) -> GameSession | None:
    puzzle = get_puzzle(puzzle_id)
    if puzzle is None:
        return None
    session = GameSession(
        puzzle_id=puzzle_id,
        question=puzzle["question"],
        answer=puzzle["answer"],
        elements=puzzle.get("elements", []),
    )
    _sessions[channel_id] = session
    return session


def get_session(channel_id: int) -> GameSession | None:
    return _sessions.get(channel_id)


def end_session(channel_id: int) -> None:
    _sessions.pop(channel_id, None)


def increment_question_count(channel_id: int) -> None:
    session = _sessions.get(channel_id)
    if session:
        session.question_count += 1


def get_score_details(channel_id: int, covered_ids: list[str]) -> tuple[list[str], list[int]]:
    """covered_ids をもとにスコア詳細を返す。
    戻り値: (到達済み要素のヒントテキスト一覧, 次に到達見込みの要素のヒント番号一覧)
    ヒント番号は elements 配列内の 1-indexed 位置。
    """
    session = _sessions.get(channel_id)
    if session is None:
        return [], []

    covered_set = set(covered_ids)
    covered_hints: list[str] = []
    next_hint_numbers: list[int] = []

    for i, element in enumerate(session.elements):
        eid = element["id"]
        if eid in covered_set:
            covered_hints.append(element["hint"])
        elif all(dep in covered_set for dep in element["depends_on"]):
            next_hint_numbers.append(i + 1)

    return covered_hints, next_hint_numbers


def get_hint_by_number(channel_id: int, number: int) -> str | None:
    """1-indexed の番号でヒントを直接返す。revealed_ids は変更しない。"""
    session = _sessions.get(channel_id)
    if session is None or number < 1 or number > len(session.elements):
        return None
    return session.elements[number - 1]["hint"]


def get_next_hint(channel_id: int) -> tuple[str, int, int, int] | None:
    """利用可能な次のヒントを1つ返し、開示済みにマークする。
    戻り値: (hint_text, hint_number, revealed_count, total_count) | None(ヒントなし)
    hint_number は elements 配列内の 1-indexed 位置。
    """
    session = _sessions.get(channel_id)
    if session is None:
        return None

    for i, element in enumerate(session.elements):
        eid = element["id"]
        if eid in session.revealed_ids:
            continue
        if all(dep in session.revealed_ids for dep in element["depends_on"]):
            session.revealed_ids.add(eid)
            total = len(session.elements)
            revealed = len(session.revealed_ids)
            return element["hint"], i + 1, revealed, total

    return None
