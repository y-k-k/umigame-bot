"""ファイル出力パスの単一窓口。

リポジトリ内のディレクトリ構成（data/ / output/ / data/sessions/）に
依存する全コードはこのモジュール経由でパスを解決する。新しい場所に書き出す
ヘルパーをここに集約することで、出力先がコード散在で増殖するのを防ぐ。

ディレクトリの意味:
- data/                 静的データ（puzzles, knowledge_base）
- data/sessions/        プロセス間共有ランタイム state（quiz_session 等）
- output/logs/<カテゴリ>/  動作確認用テキストログ
- output/battle_archives/  battle 終了時の session スナップショット
- output/logs/archive/  過去の手動ログのアーカイブ
"""
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = REPO_ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
BATTLE_SESSIONS_DIR = SESSIONS_DIR / "battle"

OUTPUT_DIR = REPO_ROOT / "output"
LOGS_DIR = OUTPUT_DIR / "logs"
BATTLE_ARCHIVES_DIR = OUTPUT_DIR / "battle_archives"

PUZZLES_FILE = DATA_DIR / "puzzles.json"
KNOWLEDGE_BASE_FILE = DATA_DIR / "knowledge_base.json"
QUIZ_SESSION_FILE = SESSIONS_DIR / "quiz_session.json"
QUIZ_SESSION_GM_FILE = SESSIONS_DIR / "quiz_session_gm.json"
NEW_PUZZLES_DRAFT_FILE = SESSIONS_DIR / "new_puzzles_draft.json"


def new_log_path(category: str, suffix: str = "") -> Path:
    """output/logs/<category>/YYYY-MM-DD_HHMMSS[<suffix>].log を返す。

    親ディレクトリは自動作成。`suffix` は識別子（例: "_id3"）を想定し、
    先頭の `_` は呼び出し側で付ける。
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"{ts}{suffix}.log"
    p = LOGS_DIR / category / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def battle_archive_path(filename: str) -> Path:
    """output/battle_archives/<filename> を返す。親ディレクトリは自動作成。"""
    BATTLE_ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    return BATTLE_ARCHIVES_DIR / filename


def battle_resume_path(puzzle_id: str | int) -> Path:
    """data/sessions/battle/resume_<id>.json を返す。親ディレクトリは自動作成。"""
    BATTLE_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return BATTLE_SESSIONS_DIR / f"resume_{puzzle_id}.json"
