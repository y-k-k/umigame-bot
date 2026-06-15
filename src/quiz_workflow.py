"""自動作問ワークフロー（作問→校閲ループ→出題準備）。

UI（Discord / CLI）に依存しないコア処理。進捗通知は reporter コールバックで、
中断確認は is_canceled コールバックで受け渡す。
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

import game_session as gs
import quiz_llm
from review_evaluator import ALL_CHECKS, ReviewResult


EventType = Literal[
    "start",         # 全体開始
    "attempt",       # 各試行開始
    "generated",     # 作問完了
    "review_start",  # 校閲開始（チェック開始の通知）
    "check_result",  # 校閲チェック1項目の結果（6回送られる）
    "reviewed",      # 校閲完了（verdict 確定）
    "prepared",      # 出題準備完了
    "error",         # 致命的エラー（中断）
    "exhausted",     # max_attempts に達して未合格
    "canceled",      # is_canceled() が True を返した
    "ready",         # 合格作品完成（最後に1回だけ送る）
]


@dataclass
class WorkflowEvent:
    type: EventType
    attempt: int = 0
    max_attempts: int = 0
    qa: dict | None = None
    reviewed: dict | None = None
    prepared: dict | None = None
    elapsed_secs: int = 0
    score: int | None = None
    reason: str = ""
    # check_result イベント専用フィールド
    check_name: str = ""
    check_passed: bool = False
    check_reason: str = ""
    # reviewed イベント専用フィールド
    verdict: str = ""


Reporter = Callable[[WorkflowEvent], Awaitable[None]]
CancelCheck = Callable[[], bool]


@dataclass
class GenerationResult:
    qa: dict
    reviewed: dict
    prepared: dict


def _noop_cancel() -> bool:
    return False


async def auto_generate(
    *,
    backend: str = "anthropic",
    model: str = "",
    max_attempts: int = 10,
    skip_review: bool = False,
    reporter: Reporter,
    is_canceled: CancelCheck = _noop_cancel,
) -> GenerationResult | None:
    """作問→校閲ループ→出題準備を実行し、合格作品を返す。

    Args:
        backend: "anthropic" or "openrouter"
        model: 空のとき backend ごとのデフォルトを使用
        max_attempts: verdict=reject 時のリトライ上限
        skip_review: True の場合、校閲ステップを丸ごとスキップし作問結果をそのまま採用する。
                     校閲 LLM が不安定な backend で生成率を上げたいときに使う。
        reporter: 進行状況を通知する async コールバック
        is_canceled: True を返したらリトライループから抜ける

    Returns:
        合格した場合: GenerationResult(qa, reviewed, prepared)
        失敗/中断時: None（reporter で理由を通知済み）
    """
    await reporter(WorkflowEvent(type="start", max_attempts=max_attempts))

    puzzles = gs.load_puzzles_raw()
    reviewed: dict | None = None
    last_qa: dict | None = None
    failed_attempts: list[str] = []

    # skip_review のときはリトライループ不要（1回だけ作問して採用）
    effective_max = 1 if skip_review else max_attempts

    for attempt in range(1, effective_max + 1):
        if is_canceled():
            await reporter(WorkflowEvent(type="canceled"))
            return None

        await reporter(WorkflowEvent(
            type="attempt", attempt=attempt, max_attempts=effective_max,
        ))

        previous_feedback = "\n".join(failed_attempts) if failed_attempts else None

        # ── 作問 ──
        t0 = time.time()
        try:
            qa = await asyncio.to_thread(
                quiz_llm.generate_quiz, puzzles, previous_feedback,
                backend=backend, model=model,
            )
        except Exception as e:
            await reporter(WorkflowEvent(type="error", reason=f"作問中にエラー: {e}"))
            return None
        gen_secs = round(time.time() - t0)
        last_qa = qa
        await reporter(WorkflowEvent(
            type="generated", attempt=attempt, max_attempts=effective_max,
            qa=qa, elapsed_secs=gen_secs,
        ))

        # ── 校閲（スキップ可）──
        if skip_review:
            reviewed = {
                "corrected_question": qa["question"],
                "corrected_answer": qa["answer"],
                "score": None,
                "reason": "校閲スキップ（skip_review=True）",
                "verdict": "adopt",
                "checks": {},
            }
            break

        await reporter(WorkflowEvent(
            type="review_start", attempt=attempt, max_attempts=effective_max,
        ))
        t0 = time.time()
        try:
            review_result: ReviewResult = await asyncio.to_thread(
                quiz_llm.review_quiz, qa["question"], qa["answer"],
                backend=backend, model=model,
            )
        except Exception as e:
            await reporter(WorkflowEvent(
                type="reviewed", attempt=attempt, max_attempts=effective_max,
                reviewed=None, elapsed_secs=round(time.time() - t0),
                reason=f"校閲応答が解析できず（{e}）— 作問からやり直し",
            ))
            failed_attempts.append(
                f"試行{attempt}: 校閲応答パース失敗"
            )
            continue
        review_secs = round(time.time() - t0)

        # チェック項目ごとに 1 イベントを送る（Discord は項目ごとに表示）
        for name in ALL_CHECKS:
            check = review_result.checks.get(name)
            if check is None:
                continue
            await reporter(WorkflowEvent(
                type="check_result",
                attempt=attempt, max_attempts=effective_max,
                check_name=name,
                check_passed=check.passed,
                check_reason=check.reason,
            ))

        v = review_result.verdict()
        reviewed = review_result.to_dict()
        reviewed["reason"] = ", ".join(
            f"{name}={'PASS' if c.passed else 'FAIL'}"
            for name, c in review_result.checks.items()
        )
        await reporter(WorkflowEvent(
            type="reviewed", attempt=attempt, max_attempts=effective_max,
            reviewed=reviewed, elapsed_secs=review_secs,
            score=review_result.score, verdict=v,
        ))

        if v in ("adopt", "revise"):
            break

        failed_attempts.append(
            f"試行{attempt}: verdict=reject / failed={review_result.failed_check_names()}"
        )
    else:
        await reporter(WorkflowEvent(type="exhausted", max_attempts=effective_max))
        return None

    assert reviewed is not None and last_qa is not None  # type narrowing

    # ── 出題準備 ──
    t0 = time.time()
    try:
        prepared = await asyncio.to_thread(
            quiz_llm.prepare_quiz,
            reviewed["corrected_question"],
            reviewed["corrected_answer"],
            backend=backend, model=model,
        )
    except Exception as e:
        await reporter(WorkflowEvent(type="error", reason=f"出題準備中にエラー: {e}"))
        return None
    prep_secs = round(time.time() - t0)
    await reporter(WorkflowEvent(
        type="prepared", prepared=prepared, elapsed_secs=prep_secs,
    ))

    result = GenerationResult(qa=last_qa, reviewed=reviewed, prepared=prepared)
    await reporter(WorkflowEvent(type="ready"))
    return result
