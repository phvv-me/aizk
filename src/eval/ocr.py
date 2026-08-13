import os
import subprocess
import unicodedata
from pathlib import Path
from typing import TypedDict

import fire
from patos import FrozenModel


class OcrPage(FrozenModel):
    """One rendered page paired with manually reviewed or native-layer truth."""

    image: Path
    truth: Path


class PageScore(FrozenModel):
    """Comparable OCR accuracy and catastrophic layout signals for one page."""

    page: str
    cer: float
    truth_characters: int
    output_characters: int
    length_ratio: float
    layout_failure: bool


class CorpusReport(FrozenModel):
    """Aggregate page-level OCR evidence without hiding incomplete corpus coverage."""

    pages: tuple[PageScore, ...]
    missing_truth: tuple[str, ...]

    @property
    def mean_cer(self) -> float:
        """Macro-average CER so one long page cannot hide shorter failures."""
        return sum(page.cer for page in self.pages) / max(1, len(self.pages))


class PageScoreReport(TypedDict):
    """JSON shape for one page score."""

    page: str
    cer: float
    truth_characters: int
    output_characters: int
    length_ratio: float
    layout_failure: bool


class OcrReport(TypedDict):
    """JSON shape returned by the probe CLI."""

    mean_cer: float
    pages: list[PageScoreReport]
    missing_truth: list[str]


class TextDistance:
    """Normalize OCR text and compute character edits with one row of state."""

    def normalize(self, text: str) -> str:
        """Fold width and remove whitespace while preserving authored characters."""
        return "".join(unicodedata.normalize("NFKC", text).split())

    def distance(self, hypothesis: str, truth: str) -> int:
        """Return Levenshtein distance without allocating the full edit matrix."""
        if hypothesis == truth:
            return 0
        if len(hypothesis) < len(truth):
            hypothesis, truth = truth, hypothesis
        previous = list(range(len(truth) + 1))
        for row, observed in enumerate(hypothesis, 1):
            current = [row]
            for column, expected in enumerate(truth, 1):
                current.append(
                    min(
                        previous[column] + 1,
                        current[column - 1] + 1,
                        previous[column - 1] + (observed != expected),
                    )
                )
            previous = current
        return previous[-1]

    def score(self, page: str, hypothesis: str, truth: str) -> PageScore:
        """Measure CER and flag output inflation that indicates repeated layout."""
        normalized_truth = self.normalize(truth)
        normalized_output = self.normalize(hypothesis)
        ratio = len(normalized_output) / max(1, len(normalized_truth))
        cer = self.distance(normalized_output, normalized_truth) / max(1, len(normalized_truth))
        return PageScore(
            page=page,
            cer=round(cer, 4),
            truth_characters=len(normalized_truth),
            output_characters=len(normalized_output),
            length_ratio=round(ratio, 4),
            layout_failure=ratio > 2.5 or cer > 1.0,
        )


class TesseractProbe:
    """Run one fixed OCR operating point against every truth-backed corpus page."""

    def __init__(
        self,
        languages: str,
        psm: int,
        timeout: int,
        tessdata: Path | None,
    ) -> None:
        self.languages = languages
        self.psm = psm
        self.timeout = timeout
        self.tessdata = tessdata
        self.distance = TextDistance()

    def pages(self, corpus: Path) -> tuple[tuple[OcrPage, ...], tuple[str, ...]]:
        """Pair images with same-stem truth and report every incomplete entry."""
        truth_root = corpus / "truth"
        matched: list[OcrPage] = []
        missing: list[str] = []
        for image in sorted(corpus.glob("*.png")):
            truth = truth_root / f"{image.stem}.txt"
            if truth.exists():
                matched.append(OcrPage(image=image, truth=truth))
            else:
                missing.append(image.name)
        return tuple(matched), tuple(missing)

    def recognize(self, page: OcrPage) -> str:
        """Run Tesseract with a hard per-page timeout and return UTF-8 text."""
        environment = None
        if self.tessdata is not None:
            environment = os.environ | {"TESSDATA_PREFIX": str(self.tessdata)}
        completed = subprocess.run(
            [
                "tesseract",
                str(page.image),
                "stdout",
                "-l",
                self.languages,
                "--psm",
                str(self.psm),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=self.timeout,
            env=environment,
        )
        return completed.stdout

    def run(self, corpus: Path) -> CorpusReport:
        """Score every complete page and preserve the missing-truth audit."""
        pages, missing = self.pages(corpus)
        scores = tuple(
            self.distance.score(
                page.image.name,
                self.recognize(page),
                page.truth.read_text(),
            )
            for page in pages
        )
        return CorpusReport(pages=scores, missing_truth=missing)


def main(
    corpus: str,
    languages: str = "jpn+eng",
    psm: int = 3,
    timeout: int = 300,
    tessdata: str | None = None,
) -> OcrReport:
    """Run the reproducible OCR probe and return JSON-serializable evidence."""
    report = TesseractProbe(
        languages=languages,
        psm=psm,
        timeout=timeout,
        tessdata=Path(tessdata) if tessdata is not None else None,
    ).run(Path(corpus))
    return {
        "mean_cer": round(report.mean_cer, 4),
        "pages": [
            {
                "page": page.page,
                "cer": page.cer,
                "truth_characters": page.truth_characters,
                "output_characters": page.output_characters,
                "length_ratio": page.length_ratio,
                "layout_failure": page.layout_failure,
            }
            for page in report.pages
        ],
        "missing_truth": list(report.missing_truth),
    }


if __name__ == "__main__":
    fire.Fire(main)
