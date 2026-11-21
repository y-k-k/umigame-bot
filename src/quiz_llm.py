"""作問・校閲・出題準備の LLM 呼び出し（複数バックエンド対応）。

Discord bot とローカル CLI の両方から利用される。
プロンプトの構築は prompts.py に集約。バックエンド振り分けは llm_backends.py に委ねる。
"""
import json

import llm_backends
from prompts import (
    build_quiz_generator_prompt,
    build_quiz_reviewer_prompt,
    build_quiz_preparer_prompt,
)
from review_evaluator import ReviewResult, parse_review_text


# Anthropic だけ Extended Thinking を使用する（OpenRouter には汎用機能なし）
_GEN_THINKING_BUDGET = 10000

# OpenRouter free 系モデルは JSON 形式を守れないことがあるため、パース失敗時はリトライ
_JSON_PARSE_MAX_ATTEMPTS = 3
# 校閲のプレーンテキスト出力もパースに失敗することがあるためリトライ
_REVIEW_PARSE_MAX_ATTEMPTS = 3


def _call_and_parse_json(
    *,
    system: str,
    user: str,
    max_tokens: int,
    backend: str,
    model: str,
    thinking_budget: int | None = None,
    expect_dict: bool = True,
) -> dict | list:
    """LLM 呼び出し → JSON パース → 期待する型の検査までを最大 N 回までリトライ。"""
    last_error: Exception | None = None
    for attempt in range(1, _JSON_PARSE_MAX_ATTEMPTS + 1):
        raw = llm_backends.call_llm(
            backend=backend,
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
        )
        try:
            parsed = llm_backends.parse_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            print(
                f"[WARN] LLM JSON パース失敗 ({attempt}/{_JSON_PARSE_MAX_ATTEMPTS}): {e} "
                f"| raw先頭200字: {raw[:200]!r}",
                flush=True,
            )
            continue
        if expect_dict and not isinstance(parsed, dict):
            last_error = ValueError(
                f"LLM response was not a JSON object (got {type(parsed).__name__})"
            )
            print(
                f"[WARN] LLM が dict ではなく {type(parsed).__name__} を返した "
                f"({attempt}/{_JSON_PARSE_MAX_ATTEMPTS})",
                flush=True,
            )
            continue
        return parsed
    assert last_error is not None
    raise last_error


def generate_quiz(
    puzzles: list[dict],
    previous_feedback: str | None = None,
    *,
    backend: str = "anthropic",
    model: str = "",
) -> dict:
    return _call_and_parse_json(  # type: ignore[return-value]
        system=build_quiz_generator_prompt(puzzles, previous_feedback),
        user="新しい問題を1つ作成してください",
        max_tokens=12000,
        backend=backend,
        model=model,
        thinking_budget=_GEN_THINKING_BUDGET if backend == "anthropic" else None,
    )


_SENTENCE_END_CHARS = "。．！？!?"


def _looks_truncated(text: str) -> bool:
    """文末が句点で終わっていないなら truncate と判定する。"""
    s = (text or "").strip()
    if not s:
        return False  # 空は元 Q&A を流用するので OK
    return s[-1] not in _SENTENCE_END_CHARS


def review_quiz(
    question: str,
    answer: str,
    *,
    backend: str = "anthropic",
    model: str = "",
) -> ReviewResult:
    """校閲ステップ。6 項目のチェックリスト形式 (compact) で結果を返す。

    完全性チェック:
      - チェック項目を 6 個全て取得できているか
      - score が取得できているか
      - corrected_question / corrected_answer が文末で切れていないか
    どれか欠けたら再生成（最大 N 回）。
    """
    last_error: Exception | None = None
    system = build_quiz_reviewer_prompt()
    user = f"## 問題\n{question}\n\n## 答え\n{answer}"
    for attempt in range(1, _REVIEW_PARSE_MAX_ATTEMPTS + 1):
        raw = llm_backends.call_llm(
            backend=backend,
            model=model,
            system=system,
            user=user,
            max_tokens=4096,
        )
        result = parse_review_text(raw)

        problems: list[str] = []
        if len(result.checks) < 6:
            problems.append(f"checks={len(result.checks)}個")
        if result.score is None:
            problems.append("score未取得")
        if _looks_truncated(result.corrected_question):
            problems.append("corrected_questionが文末切れ")
        if _looks_truncated(result.corrected_answer):
            problems.append("corrected_answerが文末切れ")

        if not problems:
            # 空欄は元 Q&A で埋める
            if not result.corrected_question:
                result.corrected_question = question
            if not result.corrected_answer:
                result.corrected_answer = answer
            return result

        last_error = ValueError(f"校閲出力に不備: {', '.join(problems)}")
        print(
            f"[WARN] 校閲応答不備 ({attempt}/{_REVIEW_PARSE_MAX_ATTEMPTS}): "
            f"{last_error} | raw末尾200字: {raw[-200:]!r}",
            flush=True,
        )
    assert last_error is not None
    raise last_error


def prepare_quiz(
    question: str,
    answer: str,
    *,
    backend: str = "anthropic",
    model: str = "",
) -> dict:
    """出題準備。LLM が dict 形式を守らないことがあるため寛容に救済する。

    救済パターン:
      - bare list（`[{...}, {...}]`）が返ったら `{"elements": [...]}` として扱う
    """
    parsed = _call_and_parse_json(  # type: ignore[assignment]
        system=build_quiz_preparer_prompt(),
        user=f"## 問題\n{question}\n\n## 答え\n{answer}",
        max_tokens=8192,
        backend=backend,
        model=model,
        expect_dict=False,
    )
    if isinstance(parsed, list):
        # 多くの場合、LLM は elements 配列を直接返している
        return {"elements": parsed}
    if not isinstance(parsed, dict):
        raise ValueError(
            f"prepare_quiz response was neither dict nor list (got {type(parsed).__name__})"
        )
    # dict の場合、elements が必須なので最低限の整合性チェック
    if "elements" not in parsed:
        # 万一 elements がトップレベルに無い場合は parsed 全体を elements と解釈してみる
        # 例: {"A": {...}, "B": {...}} のような dict 形の場合
        if all(isinstance(v, dict) for v in parsed.values()):
            return {"elements": list(parsed.values())}
    return parsed
