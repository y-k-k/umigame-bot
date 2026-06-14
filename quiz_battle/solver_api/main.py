"""
Solver API — セッション状態をリクエストで受け取り、推論のみ LLM に委ねる。
ファイル I/O は一切行わない（ステートレス）。
backend フィールドで "meridian"（Claude）か "ollama"（ローカル LLM）を選択できる。
"""
import asyncio
import json
import os
import re

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="solver API")

_meridian = anthropic.AsyncAnthropic(api_key="unused", base_url="http://127.0.0.1:3456")

OLLAMA_URL     = os.getenv("OLLAMA_URL",     "http://host.docker.internal:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL",   "qwen3:14b-q4_K_M")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "27000"))

MERIDIAN_MODEL     = os.getenv("MERIDIAN_MODEL",     "claude-sonnet-4-6")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL",   "qwen/qwen3.6-plus-preview:free")
OPENROUTER_TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "120"))


# ── LLM invocation ──────────────────────────────────────────────────────────

async def ask_llm(system: str, user: str, backend: str, model: str = "") -> str:
    if backend == "meridian":
        return await _ask_meridian(system, user)
    if backend == "openrouter":
        return await _ask_openrouter(system, user, model=model)
    return await _ask_ollama(system, user, model=model)


async def _ask_meridian(system: str, user: str) -> str:
    response = await _meridian.messages.create(
        model=MERIDIAN_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    raise RuntimeError("No text block in Meridian response")


def _parse_openrouter_response(text: str) -> str:
    """JSON形式・SSEストリーミング形式の両方に対応してcontentを取り出す。"""
    # 通常JSONレスポンス
    try:
        data = json.loads(text)
        msg = data["choices"][0]["message"]
        return msg.get("content") or msg.get("reasoning_content", "")
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    # SSE形式: "data: {...}" の各行を結合
    parts: list[str] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[6:])
            delta = chunk["choices"][0].get("delta", {})
            c = delta.get("content") or delta.get("reasoning_content", "")
            if c:
                parts.append(c)
        except Exception:
            pass
    if parts:
        return "".join(parts)
    raise RuntimeError(f"OpenRouterレスポンスを解析できません: {text[:300]}")


async def _ask_openrouter(system: str, user: str, model: str = "") -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    _max_retries = 10
    async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT) as client:
        for attempt in range(_max_retries + 1):
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": model or OPENROUTER_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                },
            )
            if r.status_code == 429:
                if attempt >= _max_retries:
                    break
                retry_after = int(r.headers.get("Retry-After", 0)) or 60
                print(f"[WARN] OpenRouter 429 — {retry_after}s後にリトライ ({attempt + 1}/{_max_retries})...", flush=True)
                await asyncio.sleep(retry_after)
                continue
            if r.status_code != 200:
                msg = f"OpenRouter {r.status_code}: {r.text[:300]}"
                print(f"[ERROR] {msg}", flush=True)
                raise RuntimeError(msg)
            try:
                return _parse_openrouter_response(r.text)
            except RuntimeError as e:
                if attempt >= _max_retries:
                    raise
                print(f"[WARN] OpenRouterレスポンス解析失敗、リトライ ({attempt + 1}/{_max_retries}): {e}", flush=True)
                await asyncio.sleep(5)
                continue
    msg = "OpenRouter 429: レート制限が続いています"
    print(f"[ERROR] {msg}", flush=True)
    raise RuntimeError(msg)


async def _ask_ollama(system: str, user: str, model: str = "") -> str:
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model or OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "stream": False,
                "keep_alive": -1,  # モデルをメモリに常駐させる
            },
        )
        if r.status_code != 200:
            msg = f"Ollama {r.status_code}: {r.text[:300]}"
            print(f"[ERROR] {msg}", flush=True)
            raise RuntimeError(msg)
        return r.json()["message"]["content"]


def strip_thinking(text: str) -> str:
    """qwen3 の <think>...</think> ブロックを除去する。"""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def extract_json(text: str) -> dict:
    text = strip_thinking(text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {text[:300]}")
    return json.loads(match.group())


def log_reasoning(raw: str) -> None:
    """JSON出力より前の構造化推論テキストをサーバーログに出力する。"""
    text = strip_thinking(raw)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    structured = text[:match.start()].strip() if match else text.strip()
    if structured:
        print(f"[SOLVER REASONING]\n{structured}\n{'─' * 60}", flush=True)


# ── Request models ───────────────────────────────────────────────────────────

class SessionState(BaseModel):
    puzzle_id: str | int
    question: str
    qa_log: list[dict]
    backend: str = "ollama"  # "ollama" | "meridian" | "openrouter"
    model: str = ""          # 空のときはバックエンドごとの環境変数を使用
    retry_count: int = 0     # 空リスト連続回数（orchestratorから渡される）
    stagnant_rounds: int = 0 # 真相究明度が更新されていない連続ラウンド数（orchestratorから渡される）


# ── Prompts ──────────────────────────────────────────────────────────────────

_QUESTIONS_SYSTEM = """あなたはウミガメのスープの水平思考クイズを解くソルバーです。
問題文とQ&Aログを分析し、次に確認すべき3〜5個のyes/no質問を生成してください。

【ターンごとの手順】

ステップ0：中心行動の特定
問題文から、説明が要求されている行動（中心行動C）を1つ特定する。
例: 自殺した、奇妙な行動を止めた、泣いた、怒った など

ステップ1：矛盾の列挙
以下から論理的に同時成立しない命題ペアを列挙する。
 - 問題文の事実同士
 - 問題文の事実 vs Q&Aで得た「はい」回答
 - Q&Aの「はい」回答同士
Q&Aログが空の場合: 問題文の事実と一般常識の間に想定される矛盾を列挙する。

ステップ2：矛盾の分類
「現在の確認済み事実だけでは中心行動Cを合理的に説明できるか？」を自問し、
説明に必要な「不足している中間ステップ」を列挙する（これらが核心的矛盾の正体）。
 核心的矛盾 = これがないと中心行動Cの説明が成立しない
 周辺的矛盾 = なくても中心行動Cを説明できる

ステップ3：説明仮説チェーンの生成
未解決の核心的矛盾を埋めるための仮説を因果連鎖として生成する。
 - 「背景（過去の出来事や状況） → きっかけ → 行動C」を基本形とし、問題に応じて中間要素を補う（チェーンは3〜6要素）
 - 各要素はYes/Noで検証可能な命題にする
 - 確認済み事実と矛盾しないこと

ステップ4：質問の生成
仮説チェーンの各要素を検証するYes/No質問を3〜5個生成する。
優先順位: ①現在の認識・感情 ②過去の重要な出来事 ③因果関係 ④具体的事実
ルール:
 - 「〜ですか？」形式
 - 1質問1情報（「〜ですか、それとも〜ですか？」などの複合質問は絶対禁止）
 - Q&Aログにある質問と同一・同義の質問は絶対禁止
 - 固有の数値・具体的な品名・細部の例を尋ねる質問は、その上位カテゴリが「はい」で確認された後のみ

ステップ5：真相究明度の計算
仮説チェーンの各要素を「裏付け済み」か「未検証」に分類する。
 - 裏付け済み = Q&Aログの「はい」回答が根拠にある要素。根拠のQ番号を必ず付記する
 - Q番号を付記できない要素はすべて未検証（もっともらしい推測でも未検証）
真相究明度 = 裏付け済み要素数 ÷ チェーン全要素数
ただし行動Cの直接の理由に当たる要素が未検証の間は、究明度の上限を0.6とする。

【出力形式】以下のセクションを順に出力した後、最後にJSONで終了すること。

【確認済みの事実】
- （Q&Aの「はい」回答から抽出。初回は「なし」）

【除外された仮説】
- （Q&Aの「いいえ」回答から抽出。初回は「なし」）

【検出された矛盾リスト】
矛盾1：「命題A」 vs 「命題B」 ― 核心的 or 周辺的
...

【不足している中間ステップ】
1. ...

【説明仮説チェーン（現在の推測）】
1. 要素の内容（裏付け=Q3 または 未検証）
...

【最重要】上記の後、必ず以下のJSON形式のみで終了すること。JSONなしで終了することは絶対に禁止。
{"pending_questions": ["質問1", "質問2", "質問3"], "comprehension": 0.65}"""

_BASIC_ELEMENTS_CHECK = """【確認すべき基本要素】（各要素がQ&Aログから判明済みか不明かを判定する）
※不明であること自体は問題ではない。中心行動Cの説明に影響しうる要素のみ質問する。
1. 登場人物・生物: 種類・数・属性（性別・年齢・職業）
2. 時代: 現代か過去・未来か
3. 現実/架空: 現実の慣習・事実か、物語・夢・架空の話か
4. 地域・場所: 日本か海外か、特殊な場所や施設か（宇宙、海、電車内、ショッピングモールなど）か
5. 職業・犯罪: 特定の職業や犯罪が関わるか
6. 未特定物品: 問題文に「あるもの」「なにか」などの未特定物品が含まれる場合、その正体のカテゴリが判明しているか
   （飲食物・衣類・電子機器・家具・調理器具・文具・乗り物・薬・お金・書類・楽器・スポーツ用品など）

【手順】
1. 問題文から未特定物品（「あるもの」「なにか」等）を抽出し、Q&Aログでカテゴリが判明しているか確認する
2. Q&Aログから各要素の判明状況を確認し、不明な要素についてのみ質問を生成する
3. 問題文から中心行動Cを再特定し、不足している中間ステップを列挙する
4. 真相究明度を計算: Q&Aの「はい」回答で裏付けられた説明要素数 ÷ 中心行動Cの説明に必要な要素数

【質問の足切り基準】
候補質問ごとに「答えが『はい』でも『いいえ』でも最終理論の本筋は変わらないか？」を自問し、
変わらない質問は破棄する。

【絶対禁止】Q&Aログにすでに存在する質問と同一または実質的に同じ意味の質問を生成すること。"""

_FALLBACK_QUESTIONS_SYSTEM = (
    "あなたはウミガメのスープの水平思考クイズを解くソルバーです。\n"
    "仮説チェーンの生成が困難なため、問題の基本要素から再出発します。\n\n"
    + _BASIC_ELEMENTS_CHECK
    + "\n\n【最重要】必ず以下のJSON形式のみで出力すること。JSONなしで終了することは絶対に禁止。\n"
    '{"pending_questions": ["質問1", "質問2"], "comprehension": 0.65}'
)

_FRAME_SHIFT_SYSTEM = """あなたはウミガメのスープの水平思考クイズを解くソルバーです。
これまでの質問では真相に近づけていないため、前提を疑って視点を転換します。

【手順】
1. Q&Aログで「いいえ」「関係ありません」が集中した仮説の方向性を列挙し、禁止リストとする。
2. 問題文のキーワードごとに、これまでと異なる解釈を1つ以上挙げる。
   観点: 別の語義、行動の目的が対人的（勝負・約束・比較・見栄）、時間の前後関係、主語や対象の取り違え。
3. 未検討の解釈から新しい説明仮説チェーンを作る（3〜6要素、Yes/Noで検証可能な命題）。
4. 新フレームを検証するyes/no質問を3〜5個生成する。禁止リストの方向・Q&Aログと同義の質問は生成しない。
5. 真相究明度 = Q&Aの「はい」で裏付けられたチェーン要素数 ÷ 全要素数

【出力形式】禁止リスト・新しい解釈・新チェーンを簡潔に列挙した後、必ず以下のJSONのみで終了すること。
{"pending_questions": ["質問1", "質問2", "質問3"], "comprehension": 0.3}"""

_PRE_SUBMIT_CHECK_SYSTEM = """あなたはウミガメのスープの水平思考クイズを解くソルバーです。
最終理論を提出する前の最終確認を行います。

【手順】
1. 問題文の不可解な点（「なぜ〜なのか」に当たる部分）をすべて列挙する。
2. 各点について、Q&Aログの「はい」回答だけを根拠に1行の説明を書く。
   根拠のQ番号を付記し、推測で補った部分は［推測］と明記する。
3. ［推測］を含む説明があれば、その推測を検証するyes/no質問を生成する（最大3問）。
4. 足切り: 答えが「はい」でも「いいえ」でも最終理論の本筋が変わらない質問は破棄する。
   Q&Aログにある質問と同一・同義の質問も破棄する。

すべての不可解な点が［推測］なしで説明できる場合のみ、空リストを返す。

【出力形式】
不可解な点ごとに「説明（根拠=Q番号 または ［推測］）」を列挙した後、JSONで終了すること。

【最重要】必ず以下のJSON形式で終了すること。
{"pending_questions": ["質問1", "質問2"]}"""

_SUBMIT_SYSTEM = """あなたはウミガメのスープの水平思考クイズを解くソルバーです。
問題文とQ&Aログ全件を踏まえ、最も整合性の高い真相を200文字以内で記述してください。

【手順】
1. 問題文から中心行動C（説明が必要な核心的な行動）を確認する
2. Q&Aの「はい」回答からすべての確認済み事実を列挙する
3. 「過去の出来事 → 心理変化 → 現在のトリガー → 認識の変化 → 行動C」の因果連鎖を構築する
4. 連鎖をもとにストーリー形式でまとめる（未判明部分は「不明」と明記）

JSONのみ返すこと: {"theory": "真相のテキスト（200文字以内）"}"""


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/questions")
async def generate_questions(state: SessionState):
    """セッション状態を受け取り、次の質問セットを返す。"""
    if state.qa_log:
        qa_text = "\n".join(
            f"Q{i+1}: {qa['q']} → {qa['a']}"
            for i, qa in enumerate(state.qa_log)
        )
        user = (
            f"問題: {state.question}\n\n"
            f"【回答済みQ&Aログ（以下と同一・同意味の質問は絶対に生成しないこと）】\n"
            f"{qa_text}\n\n"
            f"上記をすべて踏まえ、まだ確認されていない新しい観点で質問を生成してください。"
        )
    else:
        user = f"問題: {state.question}\n\nまだ質問はありません。最初の質問を生成してください。"

    use_fallback = state.retry_count >= 1
    use_frame_shift = (not use_fallback) and state.stagnant_rounds >= 3
    if use_fallback:
        print(f"[SOLVER] FALLBACK_QUESTIONS発動 (retry_count={state.retry_count})", flush=True)
    elif use_frame_shift:
        print(f"[SOLVER] FRAME_SHIFT発動 (stagnant_rounds={state.stagnant_rounds})", flush=True)
    if use_fallback:
        system = _FALLBACK_QUESTIONS_SYSTEM
    elif use_frame_shift:
        system = _FRAME_SHIFT_SYSTEM
    else:
        system = _QUESTIONS_SYSTEM
    pending: list[str] = []
    comprehension: float = 0.0
    for _attempt in range(3):
        raw = await ask_llm(system, user, state.backend, state.model)
        try:
            data = extract_json(raw)
            pending = data["pending_questions"]
            comprehension = max(0.0, min(1.0, float(data.get("comprehension", 0.0))))
            log_reasoning(raw)
            break
        except Exception as e:
            print(f"[WARN] JSON parse failed (attempt {_attempt + 1}/3): {e}", flush=True)

    # 完全一致の重複を除去（モデルが指示を守らなかった場合の安全網）
    existing_qs = {qa["q"] for qa in state.qa_log}
    pending = [q for q in pending if q not in existing_qs]

    # 全件重複のときは空リストを返す → orchestrator が graceful に submit へ移行
    return {"pending_questions": pending, "comprehension": comprehension}


@app.post("/pre_submit_check")
async def pre_submit_check(state: SessionState):
    """最終理論提出前に未解決事項を確認し、追加質問があれば返す。"""
    qa_text = "\n".join(
        f"Q{i+1}: {qa['q']} → {qa['a']}"
        for i, qa in enumerate(state.qa_log)
    ) if state.qa_log else "（まだ質問はありません）"
    user = (
        f"問題: {state.question}\n\n"
        f"【回答済みQ&Aログ（以下と同一・同意味の質問は絶対に生成しないこと）】\n"
        f"{qa_text}\n\n"
        f"上記のQ&Aログを踏まえ、最終理論提出前に確認すべき未解決事項があれば質問を生成してください。"
    )
    pending: list[str] = []
    for _attempt in range(3):
        raw = await ask_llm(_PRE_SUBMIT_CHECK_SYSTEM, user, state.backend, state.model)
        try:
            data = extract_json(raw)
            pending = data.get("pending_questions", [])
            log_reasoning(raw)
            break
        except Exception as e:
            print(f"[WARN] pre_submit_check JSON parse failed (attempt {_attempt + 1}/3): {e}", flush=True)

    existing_qs = {qa["q"] for qa in state.qa_log}
    pending = [q for q in pending if q not in existing_qs]
    print(f"[PRE_SUBMIT_CHECK] 追加質問数: {len(pending)}", flush=True)
    return {"pending_questions": pending}


@app.post("/submit")
async def submit_theory(state: SessionState):
    """セッション状態を受け取り、最終理論を返す。"""
    if not state.qa_log:
        raise HTTPException(400, "qa_log is empty — run /questions first")

    qa_text = "\n".join(f"Q: {qa['q']} → {qa['a']}" for qa in state.qa_log)
    user = f"問題: {state.question}\n\nQ&Aログ:\n{qa_text}"

    raw = await ask_llm(_SUBMIT_SYSTEM, user, state.backend, state.model)
    try:
        data = extract_json(raw)
        theory: str = data["theory"]
    except Exception as e:
        detail = f"Solver submit parse error: {e} | raw: {raw[:200]}"
        print(f"[ERROR] {detail}", flush=True)
        raise HTTPException(500, detail)

    if not theory:
        print("[ERROR] Solver returned empty theory", flush=True)
        raise HTTPException(500, "Solver returned empty theory")
    return {"theory": theory}
