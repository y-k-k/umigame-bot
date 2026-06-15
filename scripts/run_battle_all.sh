#!/bin/bash
# umigame_bot/ から実行すること
# docker compose up 済みの状態で使う
# 使い方: ./scripts/run_battle_all.sh [--solver-backend <backend>] [--model <model名>]
#   例: ./scripts/run_battle_all.sh --solver-backend ollama --model qwen3:14b
#   例: ./scripts/run_battle_all.sh --solver-backend openrouter --model google/gemini-2.5-flash

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

TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
LOG_DIR="output/logs/battle"
ARCHIVE_DIR="output/battle_archives"
mkdir -p "$LOG_DIR" "$ARCHIVE_DIR"

# puzzles.json はルートが配列。id:1 は動作確認済みのため除外
IDS=$(python -c "import json; d=json.load(open('data/puzzles.json', encoding='utf-8')); print(' '.join(str(p['id']) for p in d if p['id'] != 1))")

for ID in $IDS; do
    echo "=== クイズ ID $ID 開始 ==="

    LOG_FILE="$LOG_DIR/${TIMESTAMP}_id${ID}.log"

    python -m quiz_battle.orchestrator "$ID" \
        ${SOLVER_BACKEND_ARG:---solver-backend ollama} \
        --max-questions 100 \
        --upper-submit-threshold 100 \
        $MODEL_ARG \
        | tee "$LOG_FILE"
    orchestrator_status=${PIPESTATUS[0]}

    if [ $orchestrator_status -eq 0 ]; then
        echo "クイズ ID $ID 完了 → $LOG_FILE"
        cp "data/sessions/quiz_session.json"    "$ARCHIVE_DIR/${TIMESTAMP}_session_id${ID}.json"
        cp "data/sessions/quiz_session_gm.json" "$ARCHIVE_DIR/${TIMESTAMP}_session_gm_id${ID}.json"
    else
        echo "クイズ ID $ID 失敗（exit $orchestrator_status）"
    fi

    echo ""
done

echo "=== 全完了 ==="
