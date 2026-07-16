import hashlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from patos import FrozenModel
from pydantic import Field, TypeAdapter

from .models import BenchmarkDataset, BenchmarkMessage, BenchmarkQuestion, QuestionKind


class _MessageRow(FrozenModel):
    id: str = Field(alias="msg_node")
    content: str
    author: str
    role: str | None = None
    timestamp: datetime
    reply_to: str | None = None
    phase: str | None = Field(default=None, alias="phase_name")
    topic: str | None = None
    noise: bool = Field(default=False, alias="is_noise")
    decision: bool = Field(default=False, alias="is_decision_point")

    def in_channel(self, channel: str) -> BenchmarkMessage:
        """Attach the channel carried by the surrounding released JSON object."""
        return BenchmarkMessage(**self.model_dump(), channel=channel)


_CHANNELS = TypeAdapter(dict[str, list[_MessageRow]])
_ADAPTER_VERSION = "2"


class _QuestionRow(FrozenModel):
    id: str
    question: str
    answer: str
    asking_user: str = Field(alias="asking_user_id")

    def with_kind(self, kind: QuestionKind) -> BenchmarkQuestion:
        """Attach the question family carried by the source filename."""
        return BenchmarkQuestion(**self.model_dump(), kind=kind)


_QUESTION = TypeAdapter(_QuestionRow)


class GroupMemBench(FrozenModel):
    """Read the released GroupMemBench corpus into validated immutable records."""

    root: Path

    def load(
        self,
        domain: str,
        kinds: tuple[QuestionKind, ...] = tuple(QuestionKind),
        message_limit: int | None = None,
        question_limit: int | None = None,
    ) -> BenchmarkDataset:
        """Load one domain while preserving whether the result is only a diagnostic sample."""
        self.validate_limit("message_limit", message_limit)
        self.validate_limit("question_limit", question_limit)
        messages = self.read_messages(domain)
        selected = messages if message_limit is None else messages[:message_limit]
        unique_kinds = tuple(dict.fromkeys(kinds))
        questions = tuple(
            question
            for kind in unique_kinds
            for question in self.read_questions(domain, kind, question_limit)
        )
        return BenchmarkDataset(
            name="GroupMemBench",
            domain=domain,
            fingerprint=self.fingerprint(selected),
            messages=selected,
            questions=questions,
            complete_corpus=message_limit is None,
            sampled_questions=question_limit is not None,
        )

    @staticmethod
    def validate_limit(name: str, limit: int | None) -> None:
        """Reject negative limits instead of applying Python's surprising tail slice."""
        if limit is not None and limit < 0:
            raise ValueError(f"{name} must be nonnegative")

    @staticmethod
    def fingerprint(messages: tuple[BenchmarkMessage, ...]) -> str:
        """Hash the selected corpus so different revisions cannot share prepared state."""
        digest = hashlib.sha256()
        digest.update(_ADAPTER_VERSION.encode())
        for message in messages:
            digest.update(message.model_dump_json().encode())
            digest.update(b"\n")
        return digest.hexdigest()[:20]

    def read_messages(self, domain: str) -> tuple[BenchmarkMessage, ...]:
        """Validate all domain channels and return messages in global source-event order."""
        path = (
            self.root
            / "data"
            / "final"
            / domain
            / f"synthetic_domain_channels_rolevariants_{domain}.json"
        )
        channels = _CHANNELS.validate_json(path.read_text(encoding="utf-8"))
        messages = (
            message.in_channel(channel) for channel, rows in channels.items() for message in rows
        )
        return tuple(sorted(messages, key=lambda row: (row.timestamp, row.channel, row.id)))

    def read_questions(
        self, domain: str, kind: QuestionKind, limit: int | None
    ) -> Iterator[BenchmarkQuestion]:
        """Yield one validated question family from its released JSONL file."""
        path = self.root / "questions" / domain / f"{kind.value}.jsonl"
        with path.open(encoding="utf-8") as lines:
            for index, line in enumerate(lines):
                if limit is not None and index >= limit:
                    return
                yield _QUESTION.validate_json(line).with_kind(kind)
