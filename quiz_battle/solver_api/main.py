"""
Solver API — セッション状態をリクエストで受け取り、推論のみ LLM に委ねる。
ファイル I/O は一切行わない（ステートレス）。
backend フィールドで "meridian"（Claude）か "ollama"（ローカル LLM）を選択できる。
"""
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
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    raise RuntimeError("No text block in Meridian response")


async def _ask_openrouter(system: str, user: str, model: str = "") -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT) as client:
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
        if r.status_code != 200:
            msg = f"OpenRouter {r.status_code}: {r.text[:300]}"
            print(f"[ERROR] {msg}", flush=True)
            raise RuntimeError(msg)
        return r.json()["choices"][0]["message"]["content"]


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



# ── Request models ───────────────────────────────────────────────────────────

class SessionState(BaseModel):
    puzzle_id: str | int
    question: str
    qa_log: list[dict]
    backend: str = "ollama"  # "ollama" | "meridian" | "openrouter"
    model: str = ""          # 空のときはバックエンドごとの環境変数を使用
    retry_count: int = 0     # 空リスト連続回数（orchestratorから渡される）


# ── Prompts ──────────────────────────────────────────────────────────────────

_QUESTIONS_SYSTEM = """あなたはウミガメのスープの回答者です。
問題文とQ&Aログを分析し、次に確認すべき3〜5個のyes/no質問を生成してください。

【手順】
1. Q&Aログの「はい」回答から確認済みの事実をリストアップする
2. Q&Aログの「いいえ」回答から除外された仮説をリストアップする
3. まだ確認されていない観点を特定する
4. その観点に対する新しい質問だけを生成する
5. 上記を踏まえ、真相究明度（0.0〜1.0）を判定する

【絶対禁止】Q&Aログにすでに存在する質問と同一または実質的に同じ意味の質問を生成すること。
　違う言い回しでも、問いたい内容が同じならNG。新しい情報が得られる質問のみを生成すること。

質問戦略（優先順位）:
1. 場面の特定（男はどこで何をしていたか）
2. 登場人物の背景（過去・関係する人物）
3. 行動の動機（感情・義務・発見）
4. 比較・対比（何かと違う/同じに気づいたか）
5. 核心の詰め（仮説の真偽を1問で確認）

その他のルール:
- 「〜ですか？」形式のyes/no質問のみ
- 1問で1つの情報のみを問う（複合質問はNG）
- 情報量が最大になる質問を優先する

【真相究明度の基準】
0.0: 場面・登場人物・動機が全て不明
0.4: 場面はある程度特定できた。動機は不明
0.7: 場面と動機の大枠を把握。確認したい細部が残る
0.9: ほぼ真相を把握。理論として説明できる状態に近い
1.0: 完全把握。理論として過不足なく説明できる

【最重要】思考が終わったら必ず以下のJSON形式で出力すること。JSONなしで終了することは絶対に禁止。
{"pending_questions": ["質問1", "質問2", "質問3"], "comprehension": 0.65}"""

_FALLBACK_QUESTIONS_SYSTEM = """あなたはウミガメのスープの回答者です。
通常の質問生成ができなかったため、問題の基本要素に立ち返って質問を生成してください。

【確認すべき基本要素】
以下の5要素のうち、Q&Aログから不明なものを特定し、不明な要素に対してyes/no質問を生成してください。
各要素は単一とは限りません（例：大人と子供の両方が登場する、過去と現在の複数時系列にまたがる話など）。

1. **登場する生物・人物**: 動物・植物・人物が登場するか。登場する場合、種類・数・属性（性別・年齢・職業など）は何か
2. **時代**: 現代の話か、過去・未来の話か
3. **現実/架空**: 現実に存在する慣習・事実か。物語・神話・ゲーム・夢・都市伝説・テレビ番組・映画などの架空の話か
4. **地域・場所**: 日本の話か、海外の特定の国・地域か。地球外・海中など特別な場所があるか
5. **職業・犯罪**: 特定の職業（医者・弁護士・聖職者など）や犯罪（窃盗・殺人など）が深く関わるか

【手順】
1. Q&Aログを分析し、各要素が「判明済み」か「不明」かを判定する
2. 不明な要素についてのみyes/no質問を生成する（全要素判明済みの場合は最も不確かな要素を選ぶ）
3. 真相究明度（0.0〜1.0）を判定する

【絶対禁止】Q&Aログにすでに存在する質問と同一または実質的に同じ意味の質問を生成すること。

【最重要】必ず以下のJSON形式で出力すること。JSONなしで終了することは絶対に禁止。
{"pending_questions": ["質問1", "質問2"], "comprehension": 0.65}"""

_SUBMIT_SYSTEM = """あなたはウミガメのスープの回答者です。
問題文とQ&Aログ全件を踏まえ、最も整合性の高い真相を200文字以内で記述してください。

含めること:
- 男が実際にいた場面・状況
- なぜその行動をとったのか
- 判明していない部分があれば「不明」と明記

JSONのみ返すこと: {"theory": "最終理論のテキスト（200文字以内）"}"""


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

    system = _FALLBACK_QUESTIONS_SYSTEM if state.retry_count >= 1 else _QUESTIONS_SYSTEM
    pending: list[str] = []
    comprehension: float = 0.0
    for _attempt in range(3):
        raw = await ask_llm(system, user, state.backend, state.model)
        try:
            data = extract_json(raw)
            pending = data["pending_questions"]
            comprehension = max(0.0, min(1.0, float(data.get("comprehension", 0.0))))
            break
        except Exception as e:
            print(f"[WARN] JSON parse failed (attempt {_attempt + 1}/3): {e}", flush=True)

    # 完全一致の重複を除去（モデルが指示を守らなかった場合の安全網）
    existing_qs = {qa["q"] for qa in state.qa_log}
    pending = [q for q in pending if q not in existing_qs]

    # 全件重複のときは空リストを返す → orchestrator が graceful に submit へ移行
    return {"pending_questions": pending, "comprehension": comprehension}


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
