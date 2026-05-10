# solverエージェント — 自動作問品質改善サブタスク

## 目的

`quiz-gm`（出題者）と `solve-quiz`（回答者）の2エージェントを交互に実行し、
AIが実際にゲームを解く過程を通じて作問品質を定量評価する。

**位置づけ**: `docs/auto-quiz-quality.md` に記録されている校閲スコア（`build_quiz_reviewer_prompt`）だけでは「解かれやすさ」は測れない。solverが実際に解いてみることで「解けるが難しい」問題と「そもそも解けない問題」を区別する指標を加える。

---

## 実装済み

### スキル

| スキル | ファイル | 役割 |
|---|---|---|
| `quiz-gm` | `.claude/skills/quiz-gm/SKILL.md` | 出題者。init/回答/採点の3モードで動作 |
| `solve-quiz` | `.claude/skills/solve-quiz/SKILL.md` | 回答者。問題文とQ&Aログだけを見て質問を生成 |

### セッションファイル

| ファイル | 公開範囲 | 内容 |
|---|---|---|
| `puzzles/quiz_session.json` | GM・solver 両方が読み書き | question, qa_log, pending_questions, theory, status |
| `.claude/quiz_session_gm.json` | GMのみ | answer, elements, knowledge_base |

GM ファイルを `puzzles/` 外の `.claude/` に配置し、solve-quiz SKILL.md からはパスを一切記載しないことで、solverがファイルの存在を知る手がかりを与えない設計にしている。

---

## 手動実行フロー

```
# 1. セッション初期化（puzzles.jsonのidを指定 or draftで自動作問結果を使用）
/quiz-gm init <id>
/quiz-gm init draft

# 2. 回答エージェントが質問を生成（3〜5問）
/solve-quiz

# 3. 出題エージェントが回答（はい/いいえ/関係ありません/どちらともいえません）
/quiz-gm

# 2〜3を繰り返す（目安: 10〜30ターン）

# 4. solver が最終理論を確定
/solve-quiz submit

# 5. GMが採点
/quiz-gm score
```

### セッション状態遷移

```
in_progress → theory_submitted → scored
```

---

## 採点結果のフォーマット（quiz_session.json に保存）

```json
{
  "status": "scored",
  "score_result": {
    "covered": ["A", "B", "B1"],
    "uncovered": ["C1", "E"],
    "coverage_pct": 60,
    "questions_used": 18
  }
}
```

---

## 次のステップ

### 優先度 高

1. **動作確認** — 既存問題（id:1〜3）でフルフローを1回実行し、GMの回答精度とsolverの質問戦略を評価する

2. **フィードバック指標の定義** — score_result から gen-quiz への feedback 文字列を生成するロジックを決める

   | 指標 | 閾値 | フィードバック例 |
   |---|---|---|
   | `questions_used < 10` | 簡単すぎ | 「10問以内で解かれた。entry pointが明白すぎる可能性」 |
   | `questions_used > 30` | 解けない | 「30問超えても未解決。ヒント設計が困難な構造の可能性」 |
   | `coverage_pct < 50%` | 到達困難 | 「核心要素に到達できなかった：[E]」 |
   | `coverage_pct == 100%` | 理想的 | 「全要素到達：質問数〇問」 |

3. **gen-quiz との統合** — `auto-quiz-quality.md` の改善サイクルに solver スコアを追加する

   現在のサイクル: `gen-quiz → review-quiz (スコア≥9で採用)`
   目標サイクル: `gen-quiz → review-quiz (≥9) → solve-quiz (10〜30問で全要素到達) → 採用`

### 優先度 低

4. **quiz-battle スキル** — GM・solver の交互実行を自動化するオーケストレーターを作成（Agent toolを使用）

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| `docs/auto-quiz-quality.md` | 作問品質改善の全体ログ（このサブタスクの親） |
| `.claude/skills/quiz-gm/SKILL.md` | 出題者スキル定義 |
| `.claude/skills/solve-quiz/SKILL.md` | 回答者スキル定義 |
| `puzzles/quiz_session.json` | 実行中セッションの公開情報（solver が読む） |
| `.claude/quiz_session_gm.json` | 実行中セッションのGM専用情報（answer・elements） |
