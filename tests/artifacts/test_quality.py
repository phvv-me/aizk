from aizk.artifacts.quality import MarkdownQualityGate


def test_quality_gate_rejects_catastrophic_length_inflation() -> None:
    current = "\n".join(["申請書の日本語本文です。"] * 20)
    candidate = "\n".join([current] * 3)

    assessment = MarkdownQualityGate().assess(current, candidate)

    assert not assessment.accepted
    assert (assessment.reason or "").startswith("candidate length inflated 3.")


def test_quality_gate_rejects_truncation_and_japanese_script_loss() -> None:
    gate = MarkdownQualityGate()
    current = "これは日本語の文章です。必要な情報を詳しく説明します。" * 20

    truncated = gate.assess(current, "短い文")
    kanji_only = gate.assess(current, "申請情報確認必要詳細説明事項記録保存" * 15)

    assert not truncated.accepted
    assert "retained only" in (truncated.reason or "")
    assert not kanji_only.accepted
    assert kanji_only.reason == "candidate lost the Japanese kana signal"


def test_quality_gate_accepts_first_conversion_and_comparable_improvement() -> None:
    gate = MarkdownQualityGate()
    markdown = "日本語の本文です。" * 20

    assert gate.assess(None, markdown).accepted
    assert gate.assess(markdown, f"# 見出し\n\n{markdown}").accepted


def test_quality_gate_rejects_empty_and_repeated_candidates() -> None:
    gate = MarkdownQualityGate()
    current = "\n".join(f"Unique production line number {index}" for index in range(20))
    inflated_repetition = "\n".join(["Repeated candidate line"] * 50)
    same_size_repetition = "\n".join(["Repeated candidate line"] * 20)

    assert gate.assess(current, "").reason == "candidate removed all converted text"
    assert gate.assess(current, inflated_repetition).reason == (
        "candidate length and repeated-line inflation indicate duplicated layout"
    )
    assert gate.assess(current, same_size_repetition).reason == (
        "candidate repeats too much page text"
    )
