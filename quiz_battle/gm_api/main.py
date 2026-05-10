"""
GM API — ファイル I/O は Python で直接行い、推論のみ Meridian 経由で Claude に委ねる。
"""
import asyncio
import json
import os
import re
import subprocess
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

APP_DIR = Path("/app")
PUZZLES_FILE = APP_DIR / "puzzles" / "puzzles.json"
DRAFT_FILE = APP_DIR / "puzzles" / "new_puzzles_draft.json"
SESSION_FILE = APP_DIR / "puzzles" / "quiz_session.json"
GM_FILE = APP_DIR / ".claude" / "quiz_session_gm.json"

_meridian = anthropic.AsyncAnthropic(api_key="unused", base_url="http://127.0.0.1:3456")
MERIDIAN_MODEL = os.getenv("MERIDIAN_MODEL", "claude-sonnet-4-6")

_KEEPALIVE_INTERVAL = 180  # 3分ごとに ping


def _meridian_health() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:3456/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


async def _restart_meridian() -> None:
    """Meridianプロセスをkillして再起動する。再起動後は ~/.claude の最新認証情報を読み直す。"""
    print("[keep-alive] Meridian を再起動します...", flush=True)
    subprocess.run(["pkill", "-f", "meridian"], check=False)
    await asyncio.sleep(3)

    env = os.environ.copy()
    env["MERIDIAN_HOST"] = "127.0.0.1"
    env["MERIDIAN_PORT"] = "3456"
    subprocess.Popen(["meridian"], env=env)

    for _ in range(30):          # 最大60秒待機
        await asyncio.sleep(2)
        if _meridian_health():
            print("[keep-alive] Meridian 再起動完了", flush=True)
            return
    print("[keep-alive] Meridian 再起動タイムアウト", flush=True)


async def _meridian_keepalive() -> None:
    """Meridian の OAuth セッションをアクティブに保つ。失敗時は即座に自動再起動。"""
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            await _meridian.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            print("[keep-alive] Meridian ping OK", flush=True)
        except Exception as e:
            print(f"[keep-alive] ping failed: {e} — Meridian再起動", flush=True)
            await _restart_meridian()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_meridian_keepalive())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="quiz-gm API", lifespan=lifespan)


# ── Claude invocation ───────────────────────────────────────────────────────

async def ask_claude(system: str, user: str) -> str:
    for attempt in range(1, 4):
        try:
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
        except Exception as e:
            if attempt >= 3:
                raise
            print(f"[GM] Meridian call failed (attempt {attempt}/3): {e} — Meridian再起動してリトライ", flush=True)
            await _restart_meridian()


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {text[:300]}")
    return json.loads(match.group())


# ── Session helpers ─────────────────────────────────────────────────────────

def read_session() -> dict:
    return json.loads(SESSION_FILE.read_text(encoding="utf-8"))

def write_session(data: dict) -> None:
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def read_gm() -> dict:
    return json.loads(GM_FILE.read_text(encoding="utf-8"))

def write_gm(data: dict) -> None:
    GM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Request models ───────────────────────────────────────────────────────────

class AnswerRequest(BaseModel):
    pending_questions: list[str]

class ScoreRequest(BaseModel):
    theory: str


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/init/{puzzle_id}")
async def init_session(puzzle_id: str):
    """puzzles.json または draft から問題を読み込んでセッションを初期化する。"""
    if puzzle_id == "draft":
        draft = json.loads(DRAFT_FILE.read_text(encoding="utf-8"))
        prepared = draft.get("prepared", {})
        review = draft.get("review", {})
        generation = draft.get("generation", {})
        if prepared:
            question = review.get("corrected_question") or generation.get("question", "")
            answer = review.get("corrected_answer") or generation.get("answer", "")
            elements = prepared.get("elements", [])
        elif review:
            question = review.get("corrected_question", "")
            answer = review.get("corrected_answer", "")
            elements = []
        else:
            question = generation.get("question", "")
            answer = generation.get("answer", "")
            elements = []
    else:
        puzzles = json.loads(PUZZLES_FILE.read_text(encoding="utf-8"))
        puzzle = next((p for p in puzzles if str(p["id"]) == str(puzzle_id)), None)
        if not puzzle:
            raise HTTPException(404, f"Puzzle {puzzle_id} not found")
        question = puzzle["question"]
        answer = puzzle["answer"]
        elements = puzzle.get("elements", [])

    write_session({
        "puzzle_id": puzzle_id,
        "question": question,
        "qa_log": [],
        "pending_questions": [],
        "theory": None,
        "status": "in_progress",
    })
    write_gm({"answer": answer, "elements": elements, "knowledge_base": {}})

    return {"puzzle_id": puzzle_id, "question": question}


@app.post("/answer")
async def answer_questions(req: AnswerRequest):
    """pending_questions に対して GM として回答する。"""
    session = read_session()
    if session.get("status") != "in_progress":
        raise HTTPException(400, "session is not in_progress")
    gm = read_gm()
    answer = gm["answer"]
    kb = gm.get("knowledge_base", {})

    kb_text = ""
    if kb:
        kb_text = "\n\nナレッジベース（モデルの事前知識より優先）:\n" + "\n".join(
            f"- {k}: {v}" for k, v in kb.items()
        )

    system = f"""あなたはウミガメのスープゲームの出題者です。
真相の文章だけを根拠に、各質問に以下の4択で答えてください（それ以外の情報を加えないこと）：
- はい
- いいえ
- どちらともいえません
- 関係ありません
- 質問の意味が明確ではありません。もう少し具体的に聞いてください。

JSONのみ返すこと: {{"answers": [{{"q": "質問文", "a": "はい"}}]}}{kb_text}"""

    questions_text = "\n".join(req.pending_questions)
    raw = await ask_claude(system, f"真相: {answer}\n\n質問:\n{questions_text}")

    try:
        data = extract_json(raw)
        new_qa: list[dict] = data["answers"]
    except Exception as e:
        raise HTTPException(500, f"GM answer parse error: {e} | raw: {raw[:200]}")

    session["qa_log"].extend(new_qa)
    session["pending_questions"] = []
    write_session(session)
    return {"qa_log": session["qa_log"]}


@app.post("/score")
async def score_theory(req: ScoreRequest):
    """solver の最終理論を採点する。"""
    session = read_session()
    gm = read_gm()
    answer = gm["answer"]
    elements: list[dict] = gm.get("elements", [])

    if not elements:
        result = {
            "covered": [],
            "uncovered": [],
            "coverage_pct": 100,
            "questions_used": len(session["qa_log"]),
        }
    else:
        element_list = "\n".join(
            f"- {e['id']}: {e['label']} （hint: {e['hint']}）" for e in elements
        )
        system = """あなたはウミガメのスープゲームの採点者です。
solverの最終理論が各要素をカバーしているか判定してください。
表現が違っても要素の本質を把握していればカバー済みとします。
JSONのみ返すこと: {"covered": ["A", "B"], "uncovered": ["C"]}"""

        user = f"真相: {answer}\n\n要素:\n{element_list}\n\nSolverの理論:\n{req.theory}"
        raw = await ask_claude(system, user)

        try:
            data = extract_json(raw)
        except Exception as e:
            raise HTTPException(500, f"Score parse error: {e} | raw: {raw[:200]}")

        covered = data.get("covered", [])
        uncovered = data.get("uncovered", [])
        total = len(elements)
        result = {
            "covered": covered,
            "uncovered": uncovered,
            "coverage_pct": round(len(covered) / total * 100) if total else 0,
            "questions_used": len(session["qa_log"]),
        }

    session["theory"] = req.theory
    session["status"] = "scored"
    session["score_result"] = result
    write_session(session)
    return result


@app.get("/session")
async def get_session():
    session = read_session()
    return {
        "puzzle_id": session["puzzle_id"],
        "question": session["question"],
        "qa_log": session["qa_log"],
        "status": session["status"],
    }
