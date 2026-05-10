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

---

## 動作確認結果

### id:1「ウミガメのスープ」（正答例）

```
22問使用 → solver が全要素に到達
主な質問戦略:
  - 過去に同じスープを飲んだことがあるか → はい
  - 遭難状態だったか → はい
  - 仲間が死亡していたか → はい
  - 以前のスープが人肉製だったか → はい（9問目でたどり着く）
  - 本物のスープで正体を悟ったか → はい（12問目）
  - 罪悪感が自殺の理由か → はい（14問目）
```

22問は採用基準（10〜30問）の範囲内。全要素到達。

---

## 採用パイプライン（目標）

```
gen-quiz
  → review-quiz（スコア≥9）
  → quiz-battle（10〜30問で全要素到達）
  → 採用
```

### BattleResult の判定ロジック

```python
@property
def passed(self) -> bool:
    return 10 <= self.questions_used <= 30 and self.coverage_pct == 100

@property
def rejection_reason(self) -> str | None:
    if self.questions_used < 10:  return "簡単すぎ"
    if self.questions_used > 30:  return "難しすぎ / 解けない"
    if self.coverage_pct < 100:   return f"核心未到達（{self.coverage_pct}%）"
    return None
```

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
| `--solver-backend` | `ollama` \| `meridian` | `ollama` |
| `--max-questions N` | 最大質問数 | 100 |
| `--lower-submit-threshold N` | この問数に達するまでは必ず質問を続ける | 20 |
| `--upper-submit-threshold N` | この問数に達したら強制 submit | 100 |
| `--comprehension-threshold F` | 真相究明度がこれ以上になれば早期 submit | 0.85 |

```bash
# Ollama（デフォルト）
python -m quiz_battle.orchestrator 1
python -m quiz_battle.orchestrator draft --max-questions 120

# Meridian（session に余裕があるとき）
python -m quiz_battle.orchestrator 1 --solver-backend meridian --max-questions 30 --upper-submit-threshold 30
```

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
| 高 | 複数 id での動作確認（id:2, id:4 など） |
| 高 | `rejection_reason` → `gen-quiz` フィードバック文字列の変換ロジック |
| 高 | Discord bot の `_run_auto_generate` への統合 |
| 中 | `submit_threshold` のチューニング（現在 20問） |
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
