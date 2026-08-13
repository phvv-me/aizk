import re
import unicodedata
from collections import Counter

from patos import FrozenModel

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_MARKDOWN = re.compile(r"[`#>*_~|\[\]()]")
_SPACE = re.compile(r"\s+")


class TextQuality(FrozenModel):
    """Small deterministic signals for catastrophic conversion failures."""

    characters: int
    repeated_line_ratio: float
    cjk_characters: int
    kana_ratio: float


class CandidateAssessment(FrozenModel):
    """Explain whether one candidate is safe enough to replace a live derivative."""

    accepted: bool
    reason: str | None
    current: TextQuality
    candidate: TextQuality


class MarkdownQualityGate:
    """Reject truncation, duplication, and Japanese script loss before indexing."""

    def assess(self, current: str | None, candidate: str) -> CandidateAssessment:
        """Compare one candidate with the live text using conservative failure signals."""
        current_quality = self.measure(current or "")
        candidate_quality = self.measure(candidate)
        reason = self.rejection(current_quality, candidate_quality)
        return CandidateAssessment(
            accepted=reason is None,
            reason=reason,
            current=current_quality,
            candidate=candidate_quality,
        )

    def measure(self, markdown: str) -> TextQuality:
        """Measure normalized length, repeated lines, and Japanese script balance."""
        normalized = unicodedata.normalize("NFKC", markdown)
        text = _SPACE.sub(" ", _MARKDOWN.sub("", normalized)).strip()
        lines = [
            _SPACE.sub(" ", line).strip().casefold()
            for line in normalized.splitlines()
            if len(_SPACE.sub(" ", line).strip()) >= 12
        ]
        counts = Counter(lines)
        repeated = sum(len(line) * (count - 1) for line, count in counts.items())
        line_characters = sum(map(len, lines))
        cjk_characters = len(_CJK.findall(text))
        kana_characters = len(_KANA.findall(text))
        return TextQuality(
            characters=len(text),
            repeated_line_ratio=repeated / max(1, line_characters),
            cjk_characters=cjk_characters,
            kana_ratio=kana_characters / max(1, cjk_characters),
        )

    def rejection(self, current: TextQuality, candidate: TextQuality) -> str | None:
        """Name the first promotion rule a candidate violates, if any."""
        if current.characters == 0:
            return None
        if candidate.characters == 0:
            return "candidate removed all converted text"
        length_ratio = candidate.characters / current.characters
        if length_ratio > 2.5:
            return f"candidate length inflated {length_ratio:.2f} times"
        if length_ratio < 0.35:
            return f"candidate retained only {length_ratio:.2f} of the converted text"
        repetition_delta = candidate.repeated_line_ratio - current.repeated_line_ratio
        if length_ratio > 1.5 and repetition_delta > 0.10:
            return "candidate length and repeated-line inflation indicate duplicated layout"
        if candidate.repeated_line_ratio > 0.45 and repetition_delta > 0.10:
            return "candidate repeats too much page text"
        japanese_current = current.cjk_characters >= 20 and current.kana_ratio >= 0.02
        japanese_candidate = candidate.cjk_characters >= 20
        if (
            japanese_current
            and japanese_candidate
            and candidate.kana_ratio < current.kana_ratio * 0.35
        ):
            return "candidate lost the Japanese kana signal"
        return None
