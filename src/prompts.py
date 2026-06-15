import json

from output_paths import KNOWLEDGE_BASE_FILE


def _load_knowledge_base() -> dict:
    with open(KNOWLEDGE_BASE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _build_knowledge_section_from_dict(kb: dict) -> str:
    if not kb:
        return ""
    lines = ["## ナレッジベース（モデルの事前知識より優先すること）"]
    lines.append("以下の記述は絶対的な事実です。この内容と矛盾する知識を持っていても、必ずナレッジベースの記述を優先してください。\n")
    for keyword, entry in kb.items():
        lines.append(f"### {keyword}")
        lines.append(entry.get("description", ""))
        for fact in entry.get("facts", []):
            lines.append(f"- {fact}")
    return "\n".join(lines) + "\n\n"


def _build_knowledge_section(answer: str) -> str:
    kb = _load_knowledge_base()
    entries = [(kw, entry) for kw, entry in kb.items() if kw in answer]
    if not entries:
        return ""

    lines = ["## ナレッジベース（モデルの事前知識より優先すること）"]
    lines.append("以下の記述は絶対的な事実です。この内容と矛盾する知識を持っていても、必ずナレッジベースの記述を優先してください。\n")
    for keyword, entry in entries:
        lines.append(f"### {keyword}")
        lines.append(entry["description"])
        for fact in entry["facts"]:
            lines.append(f"- {fact}")
    return "\n".join(lines) + "\n\n"


def build_game_master_prompt(question: str, answer: str, extra_knowledge: dict | None = None) -> str:
    if extra_knowledge is not None:
        knowledge_section = _build_knowledge_section_from_dict(extra_knowledge)
    else:
        knowledge_section = _build_knowledge_section(answer)
    return f"""あなたは「ウミガメのスープ」ゲームの出題者です。

{knowledge_section}## 真相（絶対に直接教えてはいけない）
{answer}

## 問題
{question}

## あなたの役割
ユーザーからの質問に対して、以下の4つのいずれかで答えてください：
- **はい** — 事実として正しい場合
- **いいえ** — 事実として正しくない場合
- **どちらともいえません** — 真相に関連はあるが、はい/いいえでは断言できない場合（真相に記載がない・程度の問題など）
- **関係ありません** — 真相を解くのに直接関係しない場合

## 厳守ルール
- 真相を直接・間接的に明かしてはいけません
- 「はい」「いいえ」「どちらともいえません」「関係ありません」以外の余分な情報を加えてはいけません
- ただし、質問が曖昧な場合は「質問の意味が明確ではありません。もう少し具体的に聞いてください」とだけ返してください
- ユーザーが真相を完全に言い当てた場合のみ「正解です！」と答えてください
- 「〜は答えに含まれますか」「〜は真相の設定にありますか」のようなメタ質問も、真相に照らして「はい」「いいえ」で答えてください

## 判断の補足
- 真相の文章は完全な情報です。真相に書かれていない詳細はこの問題の設定に存在しないものとして扱ってください。
- 「〜に登場する〜は何ですか」「〜の中に〜はありますか」のような質問（慣習・フレーズ・物の中身を問う質問）は、真相の文章だけを根拠に判断してください。あなた自身がその慣習・フレーズについて持っている知識は使ってはいけません。
- 例外：真相に登場する人物の属性（年齢・職業・性別など）が未記載の場合のみ、真相に登場する慣習・行為から文化的常識で推測して「はい」「いいえ」で答えてください。

## 回答形式
「はい」「いいえ」「どちらともいえません」「関係ありません」のどれか一言（または上記の例外メッセージ）だけを返してください。"""


def build_progress_prompt(question: str, answer: str, user_theory: str, elements: list[dict]) -> str:
    element_lines = "\n".join(f"- {e['id']}: {e['label']}" for e in elements)
    return f"""あなたは「ウミガメのスープ」ゲームの審判です。

## 問題
{question}

## 正しい真相
{answer}

## 採点基準となる要素
{element_lines}

## ユーザーの現在の推理
{user_theory}

## タスク
ユーザーの推理が上記の各要素を理解・言及しているか判定してください。
推理が要素の内容を正しく把握していれば「カバー済み」と判断します。

以下のJSON形式のみで返してください（他の文字を含めないこと）：
{{"covered": [<カバー済みの要素IDの文字列リスト>]}}"""


def _format_puzzle_example(i: int, p: dict) -> str:
    lines = [
        f"### 例{i + 1}",
        f"**問題文**: {p['question']}",
        f"**答え**: {p['answer']}",
    ]
    if p.get("elements"):
        lines.append("**ヒント構造**:")
        for e in p["elements"]:
            dep = f"← {', '.join(e['depends_on'])}" if e["depends_on"] else "（entry point）"
            lines.append(f"  [{e['id']}] {e['hint']}　{dep}")
    return "\n".join(lines)


def build_quiz_generator_prompt(puzzles: list[dict], previous_feedback: str | None = None) -> str:
    examples = "\n\n".join(_format_puzzle_example(i, p) for i, p in enumerate(puzzles))
    feedback_section = ""
    if previous_feedback:
        feedback_section = f"\n## これまでの失敗履歴（同じ失敗を繰り返さないこと）\n{previous_feedback}\n"
    return f"""あなたは「ウミガメのスープ」の作問者です。
以下の既存問題を参考に、新しい問題を1セット作成してください。

## 高品質な既存問題の例
{examples}

## 高品質な問題が持つ唯一の原則
**「問題文の描写は全て正確だが、読者が想定する場面とは全く異なる場面にいた」**という構造のみが高評価になります。

具体例：
- 「車に乗ったのに移動しなかった」→ 車は献血車。その場で献血しただけ（「車」の種類が違った）
- 「木の板が浮いている」→ 冷凍庫の中で棒アイスが溶け、棒だけが液体に浮いている（「場所」が全く違った）
- 「箱の中に小さな人が入っている」→ 読めない漢字「囚人」の字形を口頭で説明していた（「状況」が全く違った）

この構造の特徴：
- 問題文の言葉を比喩や置き換えとして使わない（全て文字通りの意味）
- 答えを知った後「確かにその場面ならこうなる」と自然に納得できる
- 問題文から答えへ、段階的なヒントで誘導できる

## ヒント設計しやすい answer の条件

上記の「ヒント構造」を参照。高品質な answer は以下の段階に自然に分解できます：
- **entry point**（問題文の情報から発想できる手がかり）が1〜3個
- **core**（answer の核心）は複数の entry point が合流した末端に位置する

answer を書いたあと、「この answer を複数の段階的な気づき（最大7個）に分解できるか」を確認してください。
1ステップで answer の核心に辿り着ける構造は NG です（例の「ヒント構造」を参考に）。
{feedback_section}
## 禁止パターン（これをやると7点以下になる）
- 語の置き換え：「パートナー」が包丁、「川」が現像槽、「書類」がおみくじ、「踏みつぶす」が飛び石など。言葉をミスリードとして使う構造は「無理がある」と判定される
- 1ステップ推測：問題文から答えが1つの連想で辿り着ける（「名前が変わった」→登録名変更、など）
- 職業の意外性だけで解ける構造：「実は〇〇という職業/役割だった」というだけで謎が解ける場合、意外性が低く7点以下になりやすい。職業を使う場合でも、その職業特有の「物・場所・状況」の誤解と組み合わせること

## その他の作成ルール
- question と answer のみ作成すること（title・elements は不要）
- answer に登場人物の属性（年齢・性別・職業など）が謎を解くうえで重要な場合は明記すること

出力前に以下を1行ずつ確認し、確認コメントを書いてから ```json ブロックで出力してください：
1. 答えが一意に特定できるか
2. question の各描写が answer の場面で矛盾なく実際に起こりうるか（一つでも矛盾があればNG）

```json
{{"question": "...", "answer": "..."}}
```"""


def build_quiz_reviewer_prompt() -> str:
    return """あなたは「ウミガメのスープ」問題集の厳格な編集者です。
提示された問題と答えを6つのチェック項目で**厳しく**評価し、必要なら修正したうえで以下の正確な形式で出力してください。

【重要な判定姿勢】
- デフォルトは「FAIL（疑わしい）」。明確に問題なしと言える場合のみ PASS。
- 迷ったら **FAIL** とする。「微妙だが許容範囲」「ぎりぎり OK」は全て FAIL。
- 各項目を独立して判定する（他項目が PASS だから連鎖的に PASS にしない）。

## チェック項目

1. **forced_substitution**（強引な言い換え）
   問題文の語句が、答えで「実は別の意味だった」と再解釈される構造を含む場合 FAIL。
   答え本文に「『〇〇』は△△のことだった」のような言い換えが明示されていれば該当。

2. **specialty_knowledge**（専門知識必須）
   答えの核心が特定の職業・道具・工程・業界慣習の知識に依存し、それを知らない人は yes/no 質問でも辿り着けない場合 FAIL。

3. **factual_error**（事実誤認）
   答えに含まれる事実関係が現実と一致しない場合 FAIL。

4. **rare_setting**（存在レアな設定）
   答えが成立するために、一般的でない施設・状況・特殊な店舗事情の存在を前提とする場合 FAIL。

5. **non_unique_answer**（答えが一意でない）
   問題文に対し、提示された答え以外にも合理的な答えが複数成立する場合 FAIL。

6. **description_mismatch**（描写と答えの不一致）
   問題文の語句や設定と、答えで明かされる実態が食い違う場合 FAIL。

## 評価手順

【ステップ1: 分析】（必ず出力すること。LLM 自身の判定根拠になる）
- 問題文の特徴的な語句を列挙する
- 答え本文に「〇〇は△△のことだった」のような言い換えが書かれているか確認する
- 答えに登場する専門用語・業界知識を列挙する
- 他に成立しうる答えが思い浮かぶか1つ挙げる

【ステップ2: 判定】（分析結果に基づいて各項目を判定）
- 各項目 PASS / FAIL を決める
- FAIL の場合は理由を1文で（証拠となる字句を必ず引用）
- ステップ1で言い換えや専門用語が見つかっていれば、対応項目は原則 FAIL

【ステップ3: 修正・採点】
- description_mismatch などを発見したら corrected_question / corrected_answer で中立に修正する
- 問題がなければ corrected_question / corrected_answer は元のままコピーする（省略不可）
- 全体の品質を 0〜10 の整数でスコアリング（参考値）

## 出力形式（この通りの順序・キー名で出力すること）

# 分析
特徴的な語句: <問題文から抽出した語句を列挙>
言い換え検出: <答え本文に再解釈の表現があれば引用、なければ「なし」>
専門用語: <答えに含まれる業界用語を列挙、なければ「なし」>
代替解釈の有無: <別の答えが浮かぶなら1つ書く、なければ「なし」>

# 判定
forced_substitution: PASS  または  FAIL: <字句を引用した理由>
specialty_knowledge: PASS  または  FAIL: <字句を引用した理由>
factual_error: PASS  または  FAIL: <字句を引用した理由>
rare_setting: PASS  または  FAIL: <字句を引用した理由>
non_unique_answer: PASS  または  FAIL: <字句を引用した理由>
description_mismatch: PASS  または  FAIL: <字句を引用した理由>

corrected_question: <修正後の問題文 / 問題なければ元のまま>
corrected_answer: <修正後の答え / 問題なければ元のまま>
score: <0〜10の整数>

【絶対要件】
- corrected_question と corrected_answer は必ず句点（。または？）で完結した完全な文として出力すること。
- score 行は必ず出力すること。"""


def build_quiz_preparer_prompt() -> str:
    return """あなたは「ウミガメのスープ」ゲームの出題準備担当です。
提示された問題と答えをもとに、出題に必要な以下の2点を作成してください。

## 1. elements（ヒント要素）
答えに含まれていて問題文には含まれていない情報を依存グラフとして構築する（最大7個）。

各要素のフィールド:
- id: entry pointは "A" "B" "C"、派生は "B1" "B2"、核心は "E"
- label: 設計用ラベル（プレイヤーには見えない）
- hint: プレイヤー向けの文言。答えを直接明かさない。entry pointは方向性のみ、深い要素ほど具体的に
- depends_on: 前提となるidのリスト（entry pointは []）

## 2. knowledge_base
慣用句・文化的風習・特定の制度が含まれる場合のみ作成する。不要な場合は {} にする。

knowledge_baseのフォーマット:
{
  "<キーワード>": {
    "description": "概要説明",
    "facts": ["事実1", "事実2"]
  }
}

以下のJSON形式のみで返してください（他の文字を含めないこと）：
{"elements": [...], "knowledge_base": {...}}"""


def build_reveal_prompt(question: str, answer: str, elements: list[dict]) -> str:
    element_lines = "\n".join(
        f"- {e['label']}（依存: {e['depends_on'] if e['depends_on'] else 'なし'}）"
        for e in elements
    )
    return f"""以下は「ウミガメのスープ」の問題・真相・採点基準です。

## 問題
{question}

## 真相
{answer}

## 採点基準の要素
{element_lines}

## タスク
採点基準の要素を参考に、この問題の要点を箇条書きで述べてください。
要素の依存関係を踏まえつつ、内容が近いものは適宜まとめて構いません。

## 出力形式
・〜
・〜
（箇条書きのみ。前置き・後書き不要）"""
