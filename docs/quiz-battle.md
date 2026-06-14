# quiz-battle — Docker 分離環境による自動バトルシステム

## 目的

作問パイプライン（gen-quiz → review-quiz）の後段として、AIが実際に問題を解けるかどうかを
自動評価するシステム。「構造的に正しい問題」（校閲スコア≥9）と「実際に解ける問題」を区別する。

---

## アーキテクチャ

```
orchestrator.py
  │
  ├─ POST http://localhost:8001  (gm-api コンテナ)
  │    ├─ /init/{puzzle_id}   セッション初期化
  │    ├─ /answer             pending_questions に GM として回答
  │    └─ /score              solver の最終理論を採点
  │
  └─ POST http://localhost:8002  (solver-api コンテナ)
       ├─ /questions          次の質問セットを生成
       └─ /submit             最終理論を提出
```

### コンテナの役割分担

| | gm-api | solver-api |
|---|---|---|
| 役割 | 出題・回答・採点 | 質問生成・理論提出 |
| `../puzzles/` | ✅ read/write マウント | 不要（stateless） |
| `../.claude/` | ✅ フルマウント（answer/elements 込み） | ❌ マウントなし |
| Claude 認証 | `~/.claude` マウント（OAuth） | `~/.claude` マウント（OAuth） |
| `ANTHROPIC_API_KEY` | **不要** | **不要** |

solver-api は **ステートレス**（ファイル不要）。orchestrator が session state をリクエストで渡すため、
GM のプライベート情報（answer, elements）に物理的にアクセスできない。

---

## 技術スタック

| 役割 | 採用技術 |
|---|---|
| API サーバー | FastAPI + uvicorn |
| Claude 呼び出し | Meridian（Claude Code の OAuth トークン利用） |
| Python ↔ Meridian | `anthropic.AsyncAnthropic(api_key="unused", base_url="http://127.0.0.1:3456")` |
| 環境分離 | Docker Compose |

### Meridian とは

[Meridian](https://github.com/rynfar/meridian) は Claude Code の OAuth トークンを使い、
Anthropic API 互換のエンドポイントを提供するローカルプロキシ。

- `ANTHROPIC_API_KEY` の代わりに `~/.claude`（`claude login` 済み）をマウントするだけで動く
- `anthropic.AsyncAnthropic(base_url=...)` で通常の Claude API と同じ感覚で呼び出せる

---

## 設計上の決定と経緯

### 1. ファイルベースのスキル実行 → Python + Meridian に変更

**当初の設計**: FastAPI から Meridian 経由で `/quiz-gm`, `/solve-quiz` スキルを呼び出し、
Claude Code がファイルの Read/Write を行う。

**問題**: Meridian が Claude Code SDK の `query()` を呼ぶ際、ファイルツールが実行されなかった
（Meridian ログの `tools=0`）。skill 構文 `/solve-quiz` が `query()` 経由では
ファイル操作をトリガーしないことが判明。

**解決策**: ファイル I/O を Python（FastAPI）が直接担い、Claude には推論だけ任せる方式に変更。

```
旧: FastAPI → Meridian → Claude Code (Read/Write ツール) → ファイル
新: FastAPI (Python でファイル操作) → Meridian → Claude (推論のみ) → JSON 返却
```

### 2. ThinkingBlock の対処

Meridian 経由のレスポンスには Claude Code の内部思考（`ThinkingBlock`）が含まれる。
`response.content[0].text` で直接アクセスするとエラーになるため、text 属性を持つブロックを検索する。

```python
for block in response.content:
    if hasattr(block, "text"):
        return block.text
```

### 3. Extended Thinking の制限

Claude API 直接呼び出しでは `budget_tokens` で思考量を制御できる。
Meridian × Claude Code ではこの制御はできない（Claude Code が内部で自律的に thinking を使う）。
quiz-battle の用途（解かれやすさ評価）では問題なし。
作問（gen-quiz）は引き続き Claude API 直接呼び出し + Extended Thinking を使用。

### 4. solver の isolation

| 手段 | 効果 |
|---|---|
| solver-api に `.claude/` をマウントしない | `quiz_session_gm.json`（answer/elements）に物理アクセス不可 |
| session state をリクエストで渡す（ステートレス設計） | ファイルマウント自体が不要 |
| `quiz_session_gm.json` を `puzzles/` 外の `.claude/` に配置 | ファイル名を知っていてもパスが推測困難 |

### 5. クラッシュ耐性

長時間バトル中の障害に備えて以下を備える:

| 機構 | 場所 | 効果 |
|---|---|---|
| Meridian 認証 keep-alive | `gm_api/main.py` `_meridian_keepalive` | 3 分おきに 1 トークン ping。失敗時は `pkill -f meridian` で再起動して `~/.claude` を読み直す |
| `procps` パッケージ | `gm_api/Dockerfile` | `pkill` を提供（slim イメージには含まれない） |
| GM セッションの永続化 | `puzzles/quiz_session.json` をボリュームマウント | コンテナ再起動後もセッションが残る |
| orchestrator レジューム | `quiz_battle/sessions/resume_<puzzle_id>.json` | GM /answer 直後・pre_submit 直後にチェックポイント。`--resume` で復帰 |

---

## 動作確認結果

### id:1「ウミガメのスープ」

22 問使用、全要素到達。Solver は遭難状態 → 仲間の死 → 人肉スープの体験 → 罪悪感での自殺、を 14 問目までに掘り当てた。

### id:10「最強ダイエット法」（solver=nvidia/nemotron-3-ultra-550b-a55b:free）

| 項目 | 値 |
|---|---|
| 採用判定 | FAIL（75%、未到達=A「友人との勝負」） |
| 使用質問数 | 38 問（早期 submit） |
| 究明度推移 | 0 → 33 → 17 → 25 → 55 → 50 → 71 → 50 → 100% |

「コンテストの初回計量前に増量」までは到達したが、A 要素「友人との勝負」を「減量コンテスト」に一般化したため A 落ち。

### id:4「ポイ捨てする男」（solver=nvidia/nemotron-3-ultra-550b-a55b:free）

| 項目 | 値 |
|---|---|
| 採用判定 | **PASS**（100%、全要素到達） |
| 使用質問数 | 97 問 |
| FRAME_SHIFT 発動 | Round 9（stagnant_rounds=3） |
| クラッシュからのレジューム | Round 12 で gm-api 認証失効 → restart + `--resume` で復帰 |

Round 9 の FRAME_SHIFT が「熱湯」キーワードに到達させ、Round 19-20 で「カップ焼きそば」「湯切り」を直撃で確定 → 最終理論で 100% カバレッジ達成。

---

## 採用パイプライン（目標）

```
gen-quiz
  → review-quiz（スコア≥9）
  → quiz-battle（全要素到達）
  → 採用
```

### BattleResult の判定ロジック

```python
@property
def passed(self) -> bool:
    return (
        10 <= self.questions_used <= self.max_questions
        and self.coverage_pct == 100
    )

@property
def rejection_reason(self) -> str | None:
    if self.questions_used < 10:                return "簡単すぎ"
    if self.questions_used > self.max_questions: return "難しすぎ / 解けない"
    if self.coverage_pct < 100:                  return f"核心未到達（{self.coverage_pct}%、未到達: ...）"
    return None
```

「簡単すぎ」の下限は 10 問固定、「難しすぎ」の上限は `max_questions`（デフォルト 100）。

---

## 起動手順

```bash
cd quiz_battle

# ~/.claude ディレクトリのパスを設定（claude login 済みであること）
echo "CLAUDE_DIR=C:/Users/your-username/.claude" > .env

docker compose up --build
```

---

## orchestrator の使い方

```bash
python -m quiz_battle.orchestrator <puzzle_id> [options]
```

| 引数 | 説明 | デフォルト |
|---|---|---|
| `puzzle_id` | puzzles.json の id（整数）または `"draft"` | — |
| `--solver-backend` | `ollama` \| `meridian` \| `openrouter` | `ollama` |
| `--model` | 使用モデル名（省略時は backend ごとのデフォルト or solver 側の環境変数） | — |
| `--max-questions N` | 最大質問数 | 100 |
| `--lower-submit-threshold N` | この問数に達するまでは必ず質問を続ける | 20 |
| `--upper-submit-threshold N` | この問数に達したら強制 submit | 100 |
| `--comprehension-threshold F` | 真相究明度がこれ以上になれば早期 submit | 0.85 |
| `--resume` | 同一 `puzzle_id` のレジューム状態があればクラッシュ位置から再開 | off |

```bash
# Ollama（デフォルト）
python -m quiz_battle.orchestrator 1
python -m quiz_battle.orchestrator draft --max-questions 120

# Meridian（session に余裕があるとき）
python -m quiz_battle.orchestrator 1 --solver-backend meridian --max-questions 30 --upper-submit-threshold 30

# OpenRouter（free 枠の大規模モデルなど）
python -m quiz_battle.orchestrator 4 --solver-backend openrouter --model "nvidia/nemotron-3-ultra-550b-a55b:free"

# クラッシュ後に同じ puzzle_id を途中から再開
python -m quiz_battle.orchestrator 4 --solver-backend openrouter --model "..." --resume
```

### レジュームの仕組み

- orchestrator は GM `/answer` 直後と提出前チェック直後に `quiz_battle/sessions/resume_<puzzle_id>.json` を書き出す
- `--resume` 起動時は同ファイルを読み、gm-api `/session` の `qa_log` と一致することを確認してから途中から再開
- 一致しなければ警告ログを出して新規開始にフォールバック
- 採点成功時にレジュームファイルを削除する（次回はデフォルトで新規開始）

---

## Discord ボットへの統合（予定）

```python
# bot.py の _run_auto_generate に追加予定
from quiz_battle import run_battle, BattleConfig

result = await run_battle(
    "draft",
    BattleConfig(
        gm_url="http://gm-api:8001",
        solver_url="http://solver-api:8002",
    ),
)
if not result.passed:
    failure_feedback = result.rejection_reason
    # 次の作問フィードバックに渡す
```

---

## 残課題

| 優先度 | タスク |
|---|---|
| 高 | `rejection_reason` → `gen-quiz` フィードバック文字列の変換ロジック |
| 高 | Discord bot の `_run_auto_generate` への統合 |
| 中 | `lower_submit_threshold` のチューニング（現在 20問） |
| 中 | GM の語彙厳密性チューニング（カップ麺 vs カップ焼きそばのような上位カテゴリ／下位カテゴリ問題） |
| 低 | `docker compose` を bot.py から起動・停止するラッパー |

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| `quiz_battle/docker-compose.yml` | 環境定義 |
| `quiz_battle/gm_api/main.py` | GM API サーバー（Python + Meridian） |
| `quiz_battle/solver_api/main.py` | Solver API サーバー（Python + Meridian） |
| `quiz_battle/orchestrator.py` | バトル制御ロジック（bot.py から import 可） |
| `docs/solver-agent.md` | Claude Code スキル版 solver の設計（インタラクティブ用） |
| `docs/auto-quiz-quality.md` | 作問品質改善の全体ログ |
