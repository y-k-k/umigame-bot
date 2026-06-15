"""
quiz-battle orchestrator — GM APIとSolver APIを交互に呼び出してバトルを実行する。

=== CLI usage ===

    python -m quiz_battle.orchestrator <puzzle_id> [options]

    puzzle_id                   puzzles.json の id（整数）または "draft"

    --solver-backend            LLMバックエンド: "ollama"（デフォルト）| "meridian" | "openrouter"
    --model                     使用モデル名（省略時はbackendsのデフォルト or solver側の環境変数）
    --max-questions N           最大質問数（デフォルト: 100）
    --lower-submit-threshold N  この問数に達するまでは必ず質問を続ける（デフォルト: 20）
    --upper-submit-threshold N  この問数に達したら強制submit（デフォルト: 100）
    --comprehension-threshold F 真相究明度がこれ以上になれば早期submit（デフォルト: 0.85）

    例:
        python -m quiz_battle.orchestrator 1
        python -m quiz_battle.orchestrator draft --solver-backend ollama --max-questions 120
        python -m quiz_battle.orchestrator 2 --solver-backend openrouter --model google/gemini-2.5-flash
        python -m quiz_battle.orchestrator 2 --solver-backend meridian --max-questions 30 --upper-submit-threshold 30

=== Python API usage ===

    from quiz_battle.orchestrator import run_battle, BattleConfig, BattleResult

    result = await run_battle("draft")
    result = await run_battle(1, BattleConfig(solver_backend="meridian", max_questions=30))
    result = await run_battle(2, BattleConfig(solver_backend="openrouter", model="google/gemini-2.5-flash"))
    # → BattleResult(questions_used=18, coverage_pct=100, comprehension=0.95, passed=True, ...)
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from quiz_battle.backends import BACKENDS

_RESUME_DIR = Path(__file__).resolve().parents[1] / "data" / "sessions" / "battle"


def _resume_path(puzzle_id: str | int) -> Path:
    return _RESUME_DIR / f"resume_{puzzle_id}.json"


def _save_resume(puzzle_id: str | int, state: dict) -> None:
    _RESUME_DIR.mkdir(parents=True, exist_ok=True)
    _resume_path(puzzle_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_resume(puzzle_id: str | int) -> dict | None:
    path = _resume_path(puzzle_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _delete_resume(puzzle_id: str | int) -> None:
    path = _resume_path(puzzle_id)
    if path.exists():
        path.unlink()


def _log(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


@dataclass
class BattleConfig:
    gm_url: str = "http://localhost:8001"
    solver_url: str = "http://localhost:8002"
    # backends.BACKENDS のキーいずれか
    solver_backend: str = "ollama"
    model: str = ""  # 空のときはsolver側の環境変数を使用
    # Q&Aループの絶対上限（ループ回数の計算にも使用）
    max_questions: int = 100
    # この問数に達するまでは必ず質問を続ける（早期submitの lower bound）
    lower_submit_threshold: int = 20
    # この問数に達したら強制submit（早期submitの upper bound）
    upper_submit_threshold: int = 100
    # 真相究明度がこれ以上かつ lower 到達後なら早期submit
    comprehension_threshold: float = 0.85
    # HTTP タイムアウト（Ollamaの応答待ちを含む。OLLAMA_TIMEOUT より大きくすること）
    http_timeout: float = 32400.0
    # クラッシュ後のレジューム: 同一 puzzle_id のレジューム状態があれば途中から再開する
    resume: bool = False


@dataclass
class BattleResult:
    puzzle_id: str
    question: str
    theory: str
    questions_used: int
    coverage_pct: int
    max_questions: int = 100
    comprehension: float = 0.0
    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            10 <= self.questions_used <= self.max_questions
            and self.coverage_pct == 100
        )

    @property
    def rejection_reason(self) -> str | None:
        if self.questions_used < 10:
            return f"簡単すぎ（{self.questions_used}問で解答）"
        if self.questions_used > self.max_questions:
            return f"難しすぎ（{self.questions_used}問で未解答）"
        if self.coverage_pct < 100:
            missing = ", ".join(self.uncovered)
            return f"核心未到達（{self.coverage_pct}%、未到達: {missing}）"
        return None


async def run_battle(
    puzzle_id: str | int,
    config: BattleConfig | None = None,
) -> BattleResult:
    """
    指定パズルIDでGM vs solverのバトルを実行し、採点結果を返す。

    Args:
        puzzle_id: puzzles.jsonのid（整数）または "draft"
        config:    BattleConfig（省略時はデフォルト値でlocalhostに接続）
    """
    if config is None:
        config = BattleConfig()

    async with httpx.AsyncClient(timeout=config.http_timeout) as client:
        # ── 1. セッション初期化 (または再開) ─────────────────────────────
        resume_state = _load_resume(puzzle_id) if config.resume else None
        if resume_state:
            # gm-api のセッション状態が一致するかも確認する（不一致なら新規開始にフォールバック）
            try:
                gm_session = (await client.get(f"{config.gm_url}/session")).json()
            except Exception:
                gm_session = None
            if (gm_session
                    and str(gm_session.get("puzzle_id")) == str(puzzle_id)
                    and gm_session.get("qa_log") == resume_state["qa_log"]):
                question = resume_state["question"]
                qa_log = list(resume_state["qa_log"])
                comprehension = resume_state["comprehension"]
                _max_comprehension = resume_state["_max_comprehension"]
                _stagnant_rounds = resume_state["_stagnant_rounds"]
                _empty_streak = resume_state["_empty_streak"]
                _log(f"再開: {len(qa_log)}問から再開 (puzzle_id={puzzle_id}, "
                     f"comp={comprehension:.0%}, max={_max_comprehension:.0%}, "
                     f"stagnant={_stagnant_rounds})")
                _log(f"問題: {question}")
            else:
                _log("再開: 状態が gm-api セッションと不一致のため新規開始")
                resume_state = None

        if not resume_state:
            _log(f"初期化中... (puzzle_id={puzzle_id})")
            resp = await client.post(f"{config.gm_url}/init/{puzzle_id}")
            resp.raise_for_status()
            init_data = resp.json()
            question = init_data["question"]
            qa_log = []
            comprehension = 0.0
            _empty_streak = 0
            _max_comprehension = 0.0
            _stagnant_rounds = 0
            _log(f"問題: {question}")

        # ── 2. Q&Aループ ────────────────────────────────────────────────
        stop_reason = "max_questions到達"
        _MAX_EMPTY_RETRIES: int = 2

        def _checkpoint() -> None:
            _save_resume(puzzle_id, {
                "puzzle_id": str(puzzle_id),
                "question": question,
                "qa_log": qa_log,
                "comprehension": comprehension,
                "_max_comprehension": _max_comprehension,
                "_stagnant_rounds": _stagnant_rounds,
                "_empty_streak": _empty_streak,
            })

        _checkpoint()

        for _round in range(config.max_questions // 3 + 1):
            # lower以上かつ真相究明度が閾値に達したら早期submit
            if (len(qa_log) >= config.lower_submit_threshold
                    and comprehension >= config.comprehension_threshold):
                stop_reason = f"真相究明度{comprehension:.0%}到達（{len(qa_log)}問）"
                break

            # upper に達したら強制submit
            if len(qa_log) >= config.upper_submit_threshold:
                stop_reason = f"upper_submit_threshold到達（{len(qa_log)}問）"
                break

            # Solver: 質問を生成
            _log(f"Round {_round + 1}  [{len(qa_log)}問済  真相究明度:{comprehension:.0%}]  "
                 f"Solverが質問を生成中... (backend={config.solver_backend})")
            resp = await client.post(
                f"{config.solver_url}/questions",
                json={"puzzle_id": str(puzzle_id), "question": question, "qa_log": qa_log,
                      "backend": config.solver_backend, "model": config.model,
                      "retry_count": _empty_streak,
                      "stagnant_rounds": _stagnant_rounds},
            )
            resp.raise_for_status()
            solver_resp = resp.json()
            pending_questions: list[str] = solver_resp["pending_questions"]
            comprehension = max(0.0, min(1.0, float(solver_resp.get("comprehension", 0.0))))
            if comprehension > _max_comprehension:
                _max_comprehension = comprehension
                _stagnant_rounds = 0
            else:
                _stagnant_rounds += 1

            if not pending_questions:
                _empty_streak += 1
                if _empty_streak >= _MAX_EMPTY_RETRIES:
                    stop_reason = f"Solverが{_MAX_EMPTY_RETRIES}回連続で質問なし"
                    break
                _log(f"         Solverが質問なし（{_empty_streak}回目）、リトライ...")
                continue
            _empty_streak = 0

            for i, q in enumerate(pending_questions, 1):
                print(f"         Q{len(qa_log) + i}: {q}", flush=True)

            # GM: 質問に回答
            _log(f"         GMが{len(pending_questions)}問に回答中...  [真相究明度: {comprehension:.0%}]")
            resp = await client.post(
                f"{config.gm_url}/answer",
                json={"pending_questions": pending_questions},
            )
            resp.raise_for_status()
            qa_log = resp.json()["qa_log"]

            # 今ラウンドの回答を表示（直近 len(pending_questions) 件）
            for qa in qa_log[-len(pending_questions):]:
                print(f"         → {qa['a']}", flush=True)

            _checkpoint()

            if len(qa_log) >= config.max_questions:
                break

        _log(f"Q&A終了: {stop_reason}  合計{len(qa_log)}問")

        # ── 2.5. 提出前チェック ─────────────────────────────────────────
        _log("Solverが提出前チェック中（未解決事項の確認）...")
        resp = await client.post(
            f"{config.solver_url}/pre_submit_check",
            json={"puzzle_id": str(puzzle_id), "question": question, "qa_log": qa_log,
                  "backend": config.solver_backend, "model": config.model},
        )
        resp.raise_for_status()
        check_questions: list[str] = resp.json().get("pending_questions", [])
        if check_questions:
            _log(f"提出前チェック: {len(check_questions)}問の追加質問あり")
            for i, q in enumerate(check_questions, 1):
                print(f"         (追加)Q{len(qa_log) + i}: {q}", flush=True)
            resp = await client.post(
                f"{config.gm_url}/answer",
                json={"pending_questions": check_questions},
            )
            resp.raise_for_status()
            qa_log = resp.json()["qa_log"]
            for qa in qa_log[-len(check_questions):]:
                print(f"         → {qa['a']}", flush=True)
            _checkpoint()
        else:
            _log("提出前チェック: 追加質問なし")

        # ── 3. 最終理論の提出 ───────────────────────────────────────────
        _log("Solverが最終理論を構築中...")
        resp = await client.post(
            f"{config.solver_url}/submit",
            json={"puzzle_id": str(puzzle_id), "question": question, "qa_log": qa_log,
                  "backend": config.solver_backend, "model": config.model},
        )
        resp.raise_for_status()
        theory: str = resp.json()["theory"]
        _log(f"理論: {theory}")

        # ── 4. 採点 ────────────────────────────────────────────────────
        _log("採点中...")
        resp = await client.post(f"{config.gm_url}/score", json={"theory": theory})
        resp.raise_for_status()
        score = resp.json()
        _log(f"採点完了: {score.get('coverage_pct')}%  covered={score.get('covered')}  uncovered={score.get('uncovered')}")

        _delete_resume(puzzle_id)

        return BattleResult(
            puzzle_id=str(puzzle_id),
            question=question,
            theory=theory,
            questions_used=score.get("questions_used", len(qa_log)),
            coverage_pct=score.get("coverage_pct", 0),
            max_questions=config.max_questions,
            comprehension=comprehension,
            covered=score.get("covered", []),
            uncovered=score.get("uncovered", []),
        )


# ── CLI エントリポイント ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="quiz-battle orchestrator")
    parser.add_argument("puzzle_id", nargs="?", default="1",
                        help="puzzles.json の id または 'draft'")
    _backend_choices = list(BACKENDS.keys())
    parser.add_argument("--solver-backend",          choices=_backend_choices, default="ollama",
                        help=f"Solver の LLM バックエンド（デフォルト: ollama）。選択肢: {_backend_choices}")
    parser.add_argument("--model",                   type=str,   default="",
                        help="使用モデル名（省略時はsolver側の環境変数を使用）")
    parser.add_argument("--max-questions",           type=int,   default=None,
                        help=f"最大質問数（デフォルト: {BattleConfig.max_questions}）")
    parser.add_argument("--lower-submit-threshold",  type=int,   default=None,
                        help=f"早期submit の lower bound（デフォルト: {BattleConfig.lower_submit_threshold}）")
    parser.add_argument("--upper-submit-threshold",  type=int,   default=None,
                        help=f"強制submit 閾値（デフォルト: {BattleConfig.upper_submit_threshold}）")
    parser.add_argument("--comprehension-threshold", type=float, default=None,
                        help=f"早期submit の真相究明度閾値（デフォルト: {BattleConfig.comprehension_threshold}）")
    parser.add_argument("--resume", action="store_true",
                        help="同一 puzzle_id のレジューム状態があればクラッシュ位置から再開する")
    args = parser.parse_args()

    default_model = BACKENDS.get(args.solver_backend, "")
    config = BattleConfig(solver_backend=args.solver_backend, model=args.model or default_model)
    if args.max_questions is not None:
        config.max_questions = args.max_questions
    if args.lower_submit_threshold is not None:
        config.lower_submit_threshold = args.lower_submit_threshold
    if args.upper_submit_threshold is not None:
        config.upper_submit_threshold = args.upper_submit_threshold
    if args.comprehension_threshold is not None:
        config.comprehension_threshold = args.comprehension_threshold
    config.resume = args.resume

    async def main():
        model_label = config.model or f"(env default for {config.solver_backend})"
        print(f"[quiz-battle] puzzle_id={args.puzzle_id}  solver={config.solver_backend}  model={model_label}  "
              f"max={config.max_questions}  lower={config.lower_submit_threshold}  "
              f"upper={config.upper_submit_threshold}  comp={config.comprehension_threshold:.0%}")
        result = await run_battle(args.puzzle_id, config)
        print(f"\n{'='*50}")
        print(f"問題: {result.question}")
        print(f"理論: {result.theory}")
        print(f"使用質問数: {result.questions_used}  真相究明度: {result.comprehension:.0%}")
        print(f"到達率: {result.coverage_pct}%  covered={result.covered}  uncovered={result.uncovered}")
        print(f"採用: {'PASS' if result.passed else f'FAIL -- {result.rejection_reason}'}")

    asyncio.run(main())
