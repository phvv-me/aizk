import dbutil
import pytest

import eval.answerability as answerability
from eval.answerability import JudgeVerdict, judge_answerable


def test_judge_answerable_uses_llm_with_question_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, type[JudgeVerdict], int]] = []

    class StubLLM:
        async def generate(
            self,
            system: str,
            user: str,
            schema: type[JudgeVerdict],
            *,
            max_tokens: int,
        ) -> JudgeVerdict:
            calls.append((system, user, schema, max_tokens))
            return JudgeVerdict(answerable=True)

    monkeypatch.setattr(
        answerability.LLM,
        "from_settings",
        classmethod(lambda cls, config: StubLLM()),
    )

    assert dbutil.run(judge_answerable("Who owns it?", "Ada owns it.")) is True
    assert calls[0][1] == "Question.\nWho owns it?\n\nContext.\nAda owns it."
    assert calls[0][2] is JudgeVerdict
    assert calls[0][3] == 64
