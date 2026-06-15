# umigame_bot

Discord上で「ウミガメのスープ」ゲームを進行するBot。  
Claude APIをゲームマスターとして使い、質問への応答・進捗評価・ヒント提供を行う。

## ディレクトリ構成

```
src/             Discord bot 本体と作問共通コア（Python）
scripts/         ローカル CLI（gen_quiz_local.py, run_battle_all.sh）
deploy/          Dockerfile（fly.toml は仕様上ルート保持）
quiz_battle/     独立した micro-service 群（gm-api, solver-api, orchestrator）
data/            静的データ + プロセス間共有 state（後述）
output/          動作確認の成果物（後述、.gitignore）
docs/            ドキュメント
.claude/         Claude Code 設定 & skill 定義
```

## src/ 構成ファイル

| ファイル | 役割 |
|---|---|
| `src/bot.py` | Discord コマンド定義 |
| `src/game_session.py` | セッション状態管理 |
| `src/claude_client.py` | ゲームプレイ中の Anthropic API 呼び出し（出題者役・採点・答え合わせ） |
| `src/quiz_llm.py` | 作問・校閲・出題準備の LLM 呼び出し（Anthropic / OpenRouter 切替対応） |
| `src/llm_backends.py` | LLM バックエンド統一インターフェース（`call_llm()` / `parse_json()`） |
| `src/quiz_workflow.py` | 自動作問ワークフロー（作問→校閲ループ→出題準備）。Discord / CLI から共通利用 |
| `src/prompts.py` | プロンプト構築 |
| `src/review_evaluator.py` | 校閲結果のパース・採用判定 |
| `src/output_paths.py` | 全コードの出力パス窓口（data/ output/ の参照を集約） |

## データ・出力ファイル

| パス | 役割 |
|---|---|
| `data/puzzles.json` | 問題データ（gitignore） |
| `data/knowledge_base.json` | ハルシネーション回避用ナレッジベース（gitignore） |
| `data/puzzles.example.json` | サンプル（コミット対象） |
| `data/sessions/quiz_session.json` | GM ↔ solver の共有 state |
| `data/sessions/quiz_session_gm.json` | GM 専用 state（answer / elements 等） |
| `data/sessions/new_puzzles_draft.json` | 作問ワークフロー中間成果物 |
| `data/sessions/battle/resume_<id>.json` | quiz_battle のクラッシュ復帰用 state |
| `output/logs/<category>/<timestamp>.log` | 動作確認用テキストログ（後述） |
| `output/battle_archives/` | battle 終了時の session スナップショット |

### 動作確認ログの保存場所（厳守）

`python` の標準出力をリダイレクトする実行ログ、`tee` で取るログなど **読み返すだけのテキストはすべて `output/logs/<カテゴリ>/` 配下** に置く。命名は `YYYY-MM-DD_HHMMSS[_<識別子>].log`。

Python コードから書き出すときは [`src/output_paths.py`](src/output_paths.py) の `new_log_path(category, suffix="")` を使うこと（`output/logs/` 配下にディレクトリを切らずに直書きしない）。

| 種別 | 置き場所 |
|---|---|
| 実行ログ（読み返すだけ） | `output/logs/<category>/` |
| プロセス間共有ランタイム state | `data/sessions/` |
| 残しておきたいセッションスナップショット | `output/battle_archives/` |

### prompts.py を変更したとき

`src/prompts.py` の作問・校閲・出題準備プロンプトを変更した場合、対応する skill も同期すること。

| 変更した関数 | 同期先 skill |
|---|---|
| `build_quiz_generator_prompt` | `.claude/skills/gen-quiz/SKILL.md` |
| `build_quiz_reviewer_prompt` | `.claude/skills/review-quiz/SKILL.md` |
| `build_quiz_preparer_prompt` | `.claude/skills/prep-quiz/SKILL.md` |

---

## ローカル実行

| 用途 | コマンド |
|---|---|
| Discord bot | `python src/bot.py` |
| 自動作問 CLI | `python scripts/gen_quiz_local.py [--backend openrouter --model ...]` |
| quiz-battle 単発 | `python -m quiz_battle.orchestrator <puzzle_id>` |
| quiz-battle 一括 | `./scripts/run_battle_all.sh [--solver-backend ...]` |

すべてリポジトリルートを cwd として実行する。`src/bot.py` と `scripts/gen_quiz_local.py` は冒頭で `sys.path` を調整するので `PYTHONPATH` 設定不要。

## デプロイ

- **Discord bot (Fly.io)**: `fly deploy` をリポジトリルートで実行（`fly.toml` がルートにあり `deploy/Dockerfile` を参照）。
- **quiz_battle (Docker Compose)**: `quiz_battle/` ディレクトリで `docker compose up`。`../data` を bind mount するため、リポジトリルートが親に存在することを前提とする。

---

## 問題の追加手順

詳細は @docs/puzzle-addition.md を参照。

`title` / `question` / `answer` が与えられたとき、以下の順で処理する：

1. **重複チェック** — `question` が既存と一致しないか確認
2. **id 採番** — 既存の最大 id + 1
3. **answer 記述チェック** — 登場人物の属性が明記されているか
4. **elements 設計** — 依存グラフを構築
5. **ナレッジベース作成** — 慣用句・文化的風習が含まれる場合（詳細: @docs/knowledge-base.md）
6. **確認表示** — elements 設計内容をユーザーに提示
7. **JSON 追加** — `data/puzzles.json` に書き込み

---

## コマンド一覧

| コマンド | 説明 |
|---|---|
| `!問題一覧` | 問題一覧を表示 |
| `!開始 <番号>` | ゲーム開始 |
| `!ヒント` | 依存解決済みの次のヒントを順に表示（`revealed_ids` を更新） |
| `!ヒント <番号>` | 指定番号のヒントを直接表示（`revealed_ids` は更新しない） |
| `!採点 <推理>` | 到達度＋到達済みヒント＋次のステップを表示。100%到達時は自動で答え合わせ＋セッション終了 |
| `!答え合わせ` | 真相を表示してゲーム終了 |
| `!終了` | ゲームを中断 |
| `!遊び方` | 遊び方を表示 |
