"""自動作問をローカル CLI から実行する。

  python scripts/gen_quiz_local.py                                         # Anthropic
  python scripts/gen_quiz_local.py --backend openrouter                    # OpenRouter デフォルトモデル
  python scripts/gen_quiz_local.py --backend openrouter --model <model-id> # モデル指定
  python scripts/gen_quiz_local.py --backend openrouter --no-review        # 校閲省略
  python scripts/gen_quiz_local.py -o out.json                             # 結果を JSON で保存
                                                                           # 省略時は data/sessions/new_puzzles_draft.json
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

import quiz_workflow
from output_paths import NEW_PUZZLES_DRAFT_FILE

load_dotenv()


async def _print_reporter(event: quiz_workflow.WorkflowEvent) -> None:
    if event.type == "start":
        print(f"=== 自動作問開始（最大{event.max_attempts}回試行） ===")
    elif event.type == "attempt":
        print(f"\n[{event.attempt}/{event.max_attempts}] 作問中...", flush=True)
    elif event.type == "generated":
        q = (event.qa or {}).get("question", "")
        print(f"  作問完了（{event.elapsed_secs}秒）")
        print(f"    Q: {q}")
    elif event.type == "review_start":
        print(f"  校閲中...", flush=True)
    elif event.type == "check_result":
        icon = "✓" if event.check_passed else "✗"
        status = "PASS" if event.check_passed else "FAIL"
        line = f"    {icon} {event.check_name}: {status}"
        if not event.check_passed and event.check_reason:
            line += f" — {event.check_reason}"
        print(line, flush=True)
    elif event.type == "reviewed":
        if event.reviewed is None:
            print(f"  校閲失敗（{event.elapsed_secs}秒）: {event.reason}")
        else:
            score = event.score if event.score is not None else "—"
            print(f"  校閲完了（{event.elapsed_secs}秒） 判定: {event.verdict} スコア: {score}/10")
    elif event.type == "prepared":
        prepared = event.prepared if isinstance(event.prepared, dict) else {}
        elements = prepared.get("elements", []) if isinstance(prepared.get("elements"), list) else []
        print(f"\n出題準備完了（{event.elapsed_secs}秒） elements={len(elements)}")
    elif event.type == "error":
        print(f"\nERROR: {event.reason}", file=sys.stderr)
    elif event.type == "exhausted":
        print(f"\n基準に達せず終了（{event.max_attempts}回試行）", file=sys.stderr)
    elif event.type == "canceled":
        print("\nキャンセルされました", file=sys.stderr)
    elif event.type == "ready":
        print("=== 完了 ===")


async def _amain(args: argparse.Namespace) -> int:
    result = await quiz_workflow.auto_generate(
        backend=args.backend,
        model=args.model,
        max_attempts=args.max_attempts,
        skip_review=args.no_review,
        reporter=_print_reporter,
    )
    if result is None:
        return 1

    output = {
        "question": result.reviewed["corrected_question"],
        "answer": result.reviewed["corrected_answer"],
        "score": result.reviewed.get("score"),
        "verdict": result.reviewed.get("verdict"),
        "checks": result.reviewed.get("checks"),
        "elements": result.prepared.get("elements", []),
        "knowledge_base": result.prepared.get("knowledge_base"),
    }

    text = json.dumps(output, ensure_ascii=False, indent=2)
    out_path = Path(args.output) if args.output else NEW_PUZZLES_DRAFT_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"\n保存先: {out_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="自動作問をローカル実行する")
    parser.add_argument("--backend", choices=["anthropic", "openrouter"], default="anthropic",
                        help="LLM バックエンド（デフォルト: anthropic）")
    parser.add_argument("--model", default="",
                        help="モデル ID（省略時は backend のデフォルト）")
    parser.add_argument("--max-attempts", type=int, default=10,
                        help="reject 判定時のリトライ上限（デフォルト: 10）")
    parser.add_argument("--no-review", action="store_true",
                        help="校閲ステップを省略し作問結果をそのまま採用する（生成率優先）")
    parser.add_argument("-o", "--output",
                        help=f"結果を書き出す JSON ファイルパス（省略時は {NEW_PUZZLES_DRAFT_FILE.relative_to(Path(__file__).resolve().parents[1])}）")
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
