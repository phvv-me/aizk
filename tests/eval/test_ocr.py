import subprocess
from pathlib import Path

import pytest

import eval.ocr as ocr_module
from eval.ocr import CorpusReport, OcrPage, TesseractProbe, TextDistance


def test_ocr_score_separates_layout_inflation_from_character_error() -> None:
    truth = "日本語の正しい本文"
    score = TextDistance().score("page.png", truth * 3, truth)

    assert score.cer == 2.0
    assert score.length_ratio == 3.0
    assert score.layout_failure


def test_ocr_score_normalizes_width_and_whitespace() -> None:
    score = TextDistance().score("page.png", "Ａ Ｂ\nＣ", "ABC")

    assert score.cer == 0
    assert not score.layout_failure


def test_distance_handles_a_short_hypothesis_and_empty_report() -> None:
    assert TextDistance().distance("a", "abc") == 2
    assert CorpusReport(pages=(), missing_truth=()).mean_cer == 0


def test_probe_runs_complete_pages_and_reports_missing_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = tmp_path / "truth"
    truth.mkdir()
    complete = tmp_path / "complete.png"
    missing = tmp_path / "missing.png"
    complete.write_bytes(b"png")
    missing.write_bytes(b"png")
    (truth / "complete.txt").write_text("日本語")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def recognize(
        command: list[str],
        *,
        capture_output: bool,
        check: bool,
        text: bool,
        timeout: int,
        env: dict[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check, text, timeout
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 0, stdout="日本語", stderr="")

    monkeypatch.setattr(subprocess, "run", recognize)
    probe = TesseractProbe("jpn+eng", 3, 10, tmp_path / "tessdata")
    report = probe.run(tmp_path)

    assert report.mean_cer == 0
    assert report.missing_truth == ("missing.png",)
    assert calls[0][0][-2:] == ["--psm", "3"]
    assert calls[0][1] is not None
    assert calls[0][1]["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")

    plain_probe = TesseractProbe("jpn", 6, 10, None)
    plain_probe.recognize(OcrPage(image=complete, truth=truth / "complete.txt"))
    assert calls[-1][1] is None


def test_ocr_main_returns_serializable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = tmp_path / "truth"
    truth.mkdir()
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    (truth / "page.txt").write_text("truth")

    def recognize(probe: TesseractProbe, page: OcrPage) -> str:
        del probe, page
        return "truth"

    monkeypatch.setattr(TesseractProbe, "recognize", recognize)
    report = ocr_module.main(str(tmp_path))

    assert report["mean_cer"] == 0
    assert report["missing_truth"] == []
    assert report["pages"][0]["page"] == "page.png"
