# solver エージェント

## 目的

ウミガメのスープを「実際に解く側」の AI。出題者（GM）と対話して質問を生成し、最終的に真相を提出する。
`gm-api` と組み合わせて [quiz-battle](./quiz-battle.md) を構成し、作問品質の「解かれやすさ」評価を行う。

---

## 二つの実装

| | API サーバー版 | Claude Code スキル版 |
|---|---|---|
| 場所 | `quiz_battle/solver_api/main.py` | `.claude/skills/solve-quiz/SKILL.md` |
| 役割 | quiz-battle（自動評価）の Solver | インタラクティブにユーザーが回す動作確認用 |
| 状態管理 | ステートレス（リクエストで qa_log を受け取る） | `data/sessions/quiz_session.json` を読み書き |
| LLM | ollama / meridian / openrouter から選択 | Claude Code（呼び出し元） |
| 利用される文脈 | バトル自動実行 | スキル単体での手動検証 |

以下は **API サーバー版** の仕様を主に記載する。スキル版は `.claude/skills/solve-quiz/SKILL.md` を参照。

---

## API エンドポイント

```
POST /questions          次の質問セットを生成
POST /pre_submit_check   提出前の最終確認質問を生成
POST /submit             最終理論を作文
GET  /health             ヘルスチェック
```

すべて `SessionState` を JSON ボディで受け取り、ステートレスに応答する。
solver は `puzzles.json` も `answer` も `elements` も読まないため、GM のプライベート情報に物理的にアクセスできない。

### SessionState

| フィールド | 用途 |
|---|---|
| `puzzle_id` | ログ識別用 |
| `question` | 問題文 |
| `qa_log` | これまでの Q&A 全件（`[{"q": ..., "a": ...}]`） |
| `backend` | `"ollama"` / `"meridian"` / `"openrouter"` |
| `model` | 空のときは backend ごとの環境変数を使用 |
| `retry_count` | `/questions` が空応答した連続回数（orchestrator が管理） |
| `stagnant_rounds` | 真相究明度が過去最大を更新しなかった連続回数（orchestrator が管理） |

---

## 推論プロンプトの三系統

`/questions` 呼び出しごとに `retry_count` と `stagnant_rounds` を見て、以下のいずれかのプロンプトに切り替わる:

| 条件 | プロンプト | 狙い |
|---|---|---|
| `retry_count >= 1` | `_FALLBACK_QUESTIONS_SYSTEM` | チェーン生成が破綻したので基本要素チェックに退避 |
| `stagnant_rounds >= 3` | `_FRAME_SHIFT_SYSTEM` | 現フレームに袋小路。前提を疑い視点転換 |
| それ以外 | `_QUESTIONS_SYSTEM` | 通常モード（5ステップ推論） |

### 通常モード（5ステップ推論）

1. **中心行動 C の特定** — 「なぜ〜したのか」の核となる行動を 1 つ特定
2. **矛盾の列挙** — 問題文・Q&A・常識間の同時成立しない命題ペアを挙げる
3. **矛盾の分類** — 「これがないと C を説明できない」核心的矛盾 vs 周辺的矛盾
4. **説明仮説チェーンの生成** — 「背景 → きっかけ → 行動 C」を基本形に 3〜6 要素
5. **質問の生成** — チェーン各要素を検証する Yes/No 質問 3〜5 個

#### 質問生成の禁則

- 「〜ですか、それとも〜ですか？」のような複合質問は禁止
- Q&A ログにある質問と同義の質問は禁止
- 固有の数値・品名・細部の例は、その上位カテゴリが Yes 確認された後のみ

### 真相究明度（comprehension）の自己評価

`真相究明度 = 裏付け済み要素数 ÷ チェーン全要素数`

- **裏付け済み** = Q&A の「はい」回答が根拠にあり、根拠の Q 番号を付記できる要素
- **未検証** = もっともらしい推測でも、Q 番号を付記できない要素
- 「行動 C の直接の理由」に当たる要素が未検証の間は上限 0.6

主観評価を「Q 番号引用」という機械的作業に置き換えているため、Solver モデルの性能に依存しにくい。

### FALLBACK モード

通常モードのチェーン生成が破綻し空応答が出た直後に発動。
仮説チェーンを諦め、基本要素（登場人物・時代・地域・物品カテゴリなど）を素朴に確認することで進行を再開する。

### FRAME_SHIFT モード

`stagnant_rounds >= 3` で発動。
プロンプトが Solver に以下を強制する:

1. これまで「いいえ／関係ありません」が集中した方向を**禁止リスト**化
2. 問題文のキーワードに**別の語義**を当て、別解釈を 1 つ以上挙げる
   （対人的目的・時間の前後関係・主語や対象の取り違えなど）
3. 未検討の解釈から**新しい仮説チェーン**を作る
4. 禁止リスト方向と既出質問を除外して質問を生成

実例: puzzle 4（ポイ捨てする男）で `stagnant_rounds=3` を達成して発動 → 「熱湯」キーワードに到達 → 「カップ焼きそば」「湯切り」を経て 100% 到達。
発動時に `[SOLVER] FRAME_SHIFT発動 (stagnant_rounds=N)` が標準出力に出る。

---

## 提出前チェック

`/pre_submit_check` は別プロンプトで動作する:

1. 問題文の不可解な点（「なぜ〜なのか」に当たる部分）を全て列挙
2. 各点について Q&A の「はい」回答だけを根拠に 1 行説明を書く。Q 番号付記、推測補完は ［推測］ と明記
3. ［推測］が残れば、それを検証する Yes/No 質問を最大 3 問生成
4. 答えが「はい」でも「いいえ」でも本筋が変わらない質問・既出と同義の質問は破棄

`[推測]` ゼロで全ての不可解点が説明できる場合だけ空リストを返す。
追加質問があれば orchestrator が GM に答えさせて qa_log を補完してから submit する。

---

## /submit（最終理論の作文）

問題文と qa_log を踏まえ、200 文字以内で真相を作文する。
出力は `{"theory": "..."}` の JSON のみ。

---

## 出力フォーマット

`/questions` の応答は JSON のみで終わるが、JSON の前に推論セクションを出力する。
orchestrator は標準出力で `[SOLVER REASONING]` ヘッダ付きでこれを表示する:

```
【確認済みの事実】 - ...
【除外された仮説】 - ...
【検出された矛盾リスト】 矛盾1: ... 核心的
【不足している中間ステップ】 1. ...
【説明仮説チェーン（現在の推測）】
  1. 要素の内容（裏付け=Q3）
  2. 要素の内容（未検証）
  ...
{"pending_questions": [...], "comprehension": 0.65}
```

`【説明仮説チェーン】` 行には必ず `（裏付け=Q番号）` か `（未検証）` の注記が付く。
これが付いていないチェーンが出てきた場合、プロンプトが正しく機能していない（要調整）のサイン。

---

## バックエンド選択

| backend | 認証手段 | 想定モデル |
|---|---|---|
| `ollama` | ローカル Ollama | qwen3:14b など軽量モデル（デフォルト: `qwen3:14b-q4_K_M`） |
| `meridian` | `~/.claude` の Claude Code OAuth | claude-sonnet-4-6（環境変数 `MERIDIAN_MODEL`） |
| `openrouter` | `OPENROUTER_API_KEY` | 任意の OpenRouter モデル（free 含む） |

OpenRouter 呼び出しは 429（レート制限）で最大 10 回まで自動リトライする。
パース失敗時もリトライ対象。

---

## 設計上の決定

### Solver の isolation（情報非対称の保証）

- solver-api コンテナに `.claude/` を**マウントしない**
- `quiz_session_gm.json`（answer・elements）には物理的にアクセス不可
- session state（question, qa_log）はすべて orchestrator がリクエストで渡す
- **ステートレス**なので volume も不要

### 主観評価ではなく裏付け率

旧仕様は「矛盾を説明できたか」を Solver に主観判定させていたが、仮説を立てた時点で「説明済み」と扱われやすく、究明度が早期にインフレして早期 submit が頻発した。
現仕様は「Q 番号引用」という機械的作業を強制し、主観に依存しないようにしている。

### FRAME_SHIFT の存在意義

長尺の Q&A ログを積んでいくと、Solver は「これまでの仮説の延長線上」にしか質問を出せなくなりやすい。
明示的に**禁止リスト → 再解釈 → 新チェーン**のサイクルを強制することで、stagnant 状態からの脱出口を作っている。

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| `quiz_battle/solver_api/main.py` | Solver API 本体 |
| `quiz_battle/backends.py` | バックエンドのデフォルトモデル定義 |
| `quiz_battle/orchestrator.py` | GM と交互呼び出しを担うバトル制御 |
| `.claude/skills/solve-quiz/SKILL.md` | インタラクティブ版 Solver スキル |
| `docs/quiz-battle.md` | quiz-battle システム全体の設計 |
| `docs/auto-quiz-quality.md` | 自動作問品質改善の親ログ |
