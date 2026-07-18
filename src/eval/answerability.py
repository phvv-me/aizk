from patos import FrozenModel

from aizk.config import settings
from aizk.serving.extract import LLM

_JUDGE_SYSTEM = (
    "You judge whether a retrieved context answers a question. Read the question and the context\n"
    "and decide whether the context holds enough to answer it. Reply answerable true or false."
)
_JUDGE_MAX_TOKENS = 64


class JudgeVerdict(FrozenModel):
    """The judge's call on whether a recalled context answers a question."""

    answerable: bool


async def judge_answerable(question: str, context: str) -> bool:
    """Ask the LLM whether a recalled context answers a question."""
    user = f"Question.\n{question}\n\nContext.\n{context}"
    verdict = await LLM.from_settings(settings).generate(
        _JUDGE_SYSTEM,
        user,
        JudgeVerdict,
        max_tokens=_JUDGE_MAX_TOKENS,
    )
    return verdict.answerable
