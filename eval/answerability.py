from patos import FrozenModel

from aizk.serving.extract import LLM

_JUDGE_SYSTEM = (
    "You judge whether a retrieved context answers a question. Read the question and the context\n"
    "and decide whether the context holds enough to answer it. Reply answerable true or false."
)


class JudgeVerdict(FrozenModel):
    """The judge's call on whether a recalled context answers a question."""

    answerable: bool


async def judge_answerable(question: str, context: str) -> bool:
    """Ask the LLM whether a recalled context answers a question."""
    user = f"Question.\n{question}\n\nContext.\n{context}"
    verdict = await LLM.configured().generate(_JUDGE_SYSTEM, user, JudgeVerdict)
    return verdict.answerable
