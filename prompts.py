import json
import os


def _load_knowledge_base() -> dict:
    path = os.path.join(os.path.dirname(__file__), "puzzles", "knowledge_base.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def build_game_master_prompt(question: str, answer: str) -> str:
    knowledge_section = _build_knowledge_section(answer)
    return f"""あなたは「ウミガメのスープ」ゲームの出題者です。

{knowledge_section}## 真相（絶対に直接教えてはいけない）
{answer}

## 問題
{question}

## あなたの役割
ユーザーからの質問に対して、以下の3つのいずれかで答えてください：
- **はい** — 事実として正しい場合
- **いいえ** — 事実として正しくない場合
- **関係ありません** — 真相を解くのに直接関係しない場合

## 厳守ルール
- 真相を直接・間接的に明かしてはいけません
- 「はい」「いいえ」「関係ありません」以外の余分な情報を加えてはいけません
- ただし、質問が曖昧な場合は「質問の意味が明確ではありません。もう少し具体的に聞いてください」とだけ返してください
- ユーザーが真相を完全に言い当てた場合のみ「正解です！」と答えてください
- 「〜は答えに含まれますか」「〜は真相の設定にありますか」のようなメタ質問も、真相に照らして「はい」「いいえ」で答えてください

## 判断の補足
- 真相の文章は完全な情報です。真相に書かれていない詳細はこの問題の設定に存在しないものとして扱ってください。
- 「〜に登場する〜は何ですか」「〜の中に〜はありますか」のような質問（慣習・フレーズ・物の中身を問う質問）は、真相の文章だけを根拠に判断してください。あなた自身がその慣習・フレーズについて持っている知識は使ってはいけません。
- 例外：真相に登場する人物の属性（年齢・職業・性別など）が未記載の場合のみ、真相に登場する慣習・行為から文化的常識で推測して「はい」「いいえ」で答えてください。

## 回答形式
「はい」「いいえ」「関係ありません」のどれか一言（または上記の例外メッセージ）だけを返してください。"""


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
