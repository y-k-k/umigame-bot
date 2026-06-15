"""校閲結果（チェックリスト）の型定義・パース・採用判定。

校閲 LLM はプレーンテキストで以下の形式を返す:

    forced_substitution: PASS
    specialty_knowledge: FAIL: <理由を1文で>
    factual_error: PASS
    rare_setting: PASS
    non_unique_answer: PASS
    description_mismatch: PASS

    corrected_question: <修正後の問題文>
    corrected_answer: <修正後の答え>
    score: <0〜10の整数>

この出力を `parse_review_text()` でパースし、`ReviewResult` を返す。
採用判定は `verdict()` が決定論的に行う。
"""
from dataclasses import dataclass, field
from typing import Literal

CRITICAL_CHECKS: tuple[str, ...] = (
    "factual_error",
    "non_unique_answer",
    "description_mismatch",
)
SOFT_CHECKS: tuple[str, ...] = (
    "forced_substitution",
    "specialty_knowledge",
    "rare_setting",
)
ALL_CHECKS: tuple[str, ...] = SOFT_CHECKS + CRITICAL_CHECKS

CHECK_LABELS: dict[str, str] = {
    "forced_substitution":   "強引な言い換え",
    "specialty_knowledge":   "専門知識必須",
    "factual_error":         "事実誤認",
    "rare_setting":          "存在レアな設定",
    "non_unique_answer":     "答えが一意でない",
    "description_mismatch":  "描写と答えの不一致",
}


Verdict = Literal["adopt", "revise", "reject"]


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class ReviewResult:
    checks: dict[str, CheckResult] = field(default_factory=dict)
    corrected_question: str = ""
    corrected_answer: str = ""
    score: int | None = None
    raw_text: str = ""

    def verdict(self) -> Verdict:
        critical_fail = any(
            name in self.checks and not self.checks[name].passed
            for name in CRITICAL_CHECKS
        )
        if critical_fail:
            return "reject"
        soft_fail = sum(
            1 for name in SOFT_CHECKS
            if name in self.checks and not self.checks[name].passed
        )
        if soft_fail == 0:
            return "adopt"
        if soft_fail == 1:
            return "revise"
        return "reject"

    def failed_check_names(self) -> list[str]:
        return [name for name, c in self.checks.items() if not c.passed]

    def to_dict(self) -> dict:
        """gen_quiz_local 等で JSON 保存するための辞書化。"""
        return {
            "checks": {
                name: {"passed": c.passed, "reason": c.reason}
                for name, c in self.checks.items()
            },
            "verdict": self.verdict(),
            "corrected_question": self.corrected_question,
            "corrected_answer": self.corrected_answer,
            "score": self.score,
        }


def parse_review_text(text: str) -> ReviewResult:
    """LLM が返したプレーンテキストをパースする。

    寛容なパーサー: 行頭の空白・大文字小文字の揺れ・余計な装飾文字 (`**`, `-` 等) を許容。
    認識できなかった行は単に無視する。
    """
    result = ReviewResult(raw_text=text)
    lines = text.splitlines()

    correction_buffer: dict[str, list[str]] = {
        "corrected_question": [],
        "corrected_answer": [],
    }
    active_correction: str | None = None

    for raw_line in lines:
        line = _strip_decor(raw_line)
        if not line:
            active_correction = None
            continue

        key, _, value = line.partition(":")
        key_norm = key.strip().lower()
        value = value.strip()

        if key_norm in ALL_CHECKS:
            active_correction = None
            verdict_token = value.upper()
            passed = verdict_token.startswith("PASS")
            reason = ""
            if not passed:
                # 「FAIL: 理由」「FAIL — 理由」など色々あり得るので分割
                for sep in (":", "-", "—", "ー"):
                    if sep in value:
                        reason = value.split(sep, 1)[1].strip()
                        break
                else:
                    # FAIL のみの場合は理由なし
                    reason = ""
            result.checks[key_norm] = CheckResult(
                name=key_norm, passed=passed, reason=reason,
            )
            continue

        if key_norm in correction_buffer:
            active_correction = key_norm
            correction_buffer[key_norm].append(value)
            continue

        if key_norm == "score":
            active_correction = None
            try:
                result.score = int(value.split()[0])
            except (ValueError, IndexError):
                pass
            continue

        # アクティブな correction フィールドに継続行を追加
        if active_correction is not None:
            correction_buffer[active_correction].append(line)

    result.corrected_question = " ".join(
        s for s in correction_buffer["corrected_question"] if s
    ).strip()
    result.corrected_answer = " ".join(
        s for s in correction_buffer["corrected_answer"] if s
    ).strip()

    return result


def _strip_decor(line: str) -> str:
    """行頭の `*` `-` `#` 等の装飾文字と空白を除去する。"""
    s = line.strip()
    while s and s[0] in "*-#>•·":
        s = s[1:].lstrip()
    while s.endswith("**"):
        s = s[:-2].rstrip()
    return s
