"""
Solver バックエンドの定義。
プロバイダー名 → デフォルトモデル名のマッピングを管理する。
"""

BACKENDS: dict[str, str] = {
    "ollama":      "qwen3:14b-q4_K_M",
    "meridian":    "",                    # Claude Code の認証情報を使用。モデル選択不可
    "openrouter":  "qwen/qwen3.6-plus-preview:free",
}
