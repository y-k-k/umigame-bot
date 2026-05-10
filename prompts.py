import json
import os

def _load_knowledge_base() -> dict:
    path = os.path.join(os.path.dirname(__file__), "puzzles", "knowledge_base.json")
    with open(path, encoding="utf-8") as f:
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
    return """あなたは「ウミガメのスープ」問題集の編集者です。
提示された問題と答えを客観的に評価し、必要に応じて修正してください。

## 評価手順
1. 以下のいずれかに該当する場合は修正する
   - 不適切な表現（差別的・過度に暴力的など）
   - question の登場人物の描写が answer の実態と食い違っている（例：answer では食品サンプル職人なのに question で「料理人」と呼んでいるなど）。ウミガメのスープの謎として成立するよう、職業・属性を answer に合わせた中立的な表現に修正する
2. 修正後の問題・答えを以下の基準で10点満点で採点する

## 採点基準
- ウミガメのスープとして成立しているか（問題文に謎がある、答えで謎が解ける）
- 問題文だけでは答えが予測しにくいか
- 答えが論理的に一意に特定できるか（他の解釈が成立しない）
- 答えに登場人物の属性（年齢・性別・職業など）が明記されているか
- ヒントが複数の段階に自然に分解できる構造か（上限7個）
- 問題文の言葉を比喩・置き換えとして使っていないか（全て文字通りの意味か）
- 専門知識なしで辿り着けるか：答えの核心となる職業・慣習・工程を知らない人でも、yes/no 質問を重ねれば段階的に辿り着ける構造か（その知識がなければ正しい質問を思いつけない場合は減点）
- 設定・前提の自然さ：答えが成立するための前提が現実に起こりうるか。以下の場合は減点
  - 事実誤認：答えに含まれる事実関係が現実と一致しない（例：108を逆さまに読んでも180にはならない）
  - 存在レア：答えが成立するために一般的でない施設・状況の存在を前提とする（例：砂浜ごと屋内に収めた施設）
- 文章の長さはマイナス評価しない

## スコアの目安
- 9〜10点：問題文の描写が全て正確で、答えを知った後に「確かにその場面ならこうなる」と完全に納得できる。一意性が高く、ヒント設計も自然にできる
- 8点：構造は成立しており、欠点は1つのみ（語の置き換えが若干強引、やや推測しやすい、または一意性がやや弱いなど）
- 7点：欠点が2つ以上ある、または語の置き換えが謎の中心を担っており強引さが際立つ
- 5〜6点：謎は成立するが、論理の飛躍、事実誤認、一般的でない施設・状況への依存、業界固有の専門知識が必須、または複数の偶然の重なりがある
- 4点以下：謎として成立しない、または答えが複数考えられる

以下のJSON形式のみで返してください（他の文字を含めないこと）：
{"corrected_question": "...", "corrected_answer": "...", "score": <0〜10の整数>, "reason": "評価理由を1〜2文で"}"""


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
