"""
Solver バックエンドの定義。
プロバイダー名 → デフォルトモデル名のマッピングを管理する。
"""

BACKENDS: dict[str, str] = {
    "ollama":      "qwen3:14b-q4_K_M",
    "meridian":    "claude-sonnet-4-6",   # Claude Code の認証情報を使用
    "openrouter":  "qwen/qwen3.6-plus-preview:free",
}
