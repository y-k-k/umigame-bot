#!/bin/bash
# umigame_bot/ から実行すること
# docker compose up 済みの状態で使う
# 使い方: ./run_battle_all.sh [--solver-backend <backend>] [--model <model名>]
#   例: ./run_battle_all.sh --solver-backend ollama --model qwen3:14b
#   例: ./run_battle_all.sh --solver-backend openrouter --model google/gemini-2.5-flash

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

SOLVER_BACKEND_ARG=""
MODEL_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --solver-backend)
            SOLVER_BACKEND_ARG="--solver-backend $2"
            shift 2
            ;;
        --model)
            MODEL_ARG="--model $2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

OUTPUT_DIR="results"
mkdir -p "$OUTPUT_DIR"

# puzzles.json はルートが配列。id:1 は動作確認済みのため除外
IDS=$(python -c "import json; d=json.load(open('puzzles/puzzles.json', encoding='utf-8')); print(' '.join(str(p['id']) for p in d if p['id'] != 1))")

for ID in $IDS; do
    echo "=== クイズ ID $ID 開始 ==="

    LOG_FILE="$OUTPUT_DIR/log_id$ID.txt"

    python -m quiz_battle.orchestrator "$ID" \
        ${SOLVER_BACKEND_ARG:---solver-backend ollama} \
        --max-questions 100 \
        --upper-submit-threshold 100 \
        $MODEL_ARG \
        | tee "$LOG_FILE"
    orchestrator_status=${PIPESTATUS[0]}

    if [ $orchestrator_status -eq 0 ]; then
        echo "クイズ ID $ID 完了 → $LOG_FILE"
        cp "puzzles/quiz_session.json"    "$OUTPUT_DIR/session_id$ID.json"
        cp ".claude/quiz_session_gm.json" "$OUTPUT_DIR/session_gm_id$ID.json"
    else
        echo "クイズ ID $ID 失敗（exit $orchestrator_status）"
    fi

    echo ""
done

echo "=== 全完了 ==="
