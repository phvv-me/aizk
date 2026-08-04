import dbutil
import httpx
import pytest
from web_doubles import ScriptedGate, as_gate

from aizk.config import settings
from aizk.web import QuerySanitizer, Refusal


def sanitizer(gate: ScriptedGate, roster: frozenset[str] = frozenset()) -> QuerySanitizer:
    """A sanitizer over a faked detector and one explicit roster."""
    return QuerySanitizer.build(settings, as_gate(gate), roster)


def test_a_clean_public_question_is_allowed_through() -> None:
    gate = ScriptedGate()

    assert dbutil.run(sanitizer(gate).refuses("how does postgres row security work")) is None
    assert gate.calls[0][2] == settings.web_search_detector_threshold


@pytest.mark.parametrize(
    "rewritten",
    [
        "how do I configure row security",
        "what did we decide about row security",
        "how does OUR deployment do this",
        "remind me of my own notes",
    ],
)
def test_a_rewrite_still_speaking_in_the_first_person_never_leaves(rewritten: str) -> None:
    gate = ScriptedGate()

    assert dbutil.run(sanitizer(gate).refuses(rewritten)) is Refusal.sanitizer_refused
    assert gate.calls == []


def test_a_rewrite_still_naming_something_the_caller_stores_never_leaves() -> None:
    gate = ScriptedGate()

    verdict = dbutil.run(sanitizer(gate, frozenset({"atlas"})).refuses("how does Atlas scale"))

    assert verdict is Refusal.sanitizer_refused
    assert gate.calls == []


def test_a_roster_name_too_short_to_identify_anyone_is_left_to_the_detector() -> None:
    gate = ScriptedGate()

    verdict = dbutil.run(sanitizer(gate, frozenset({"ai"})).refuses("how does a plain thing work"))

    assert verdict is None
    assert gate.calls != []


def test_the_detector_gets_the_last_word_even_on_a_rewrite_nothing_else_caught() -> None:
    gate = ScriptedGate(mentions=["borealis"])

    verdict = dbutil.run(sanitizer(gate).refuses("how does borealis scale"))

    assert verdict is Refusal.sanitizer_refused
    assert gate.calls[0][1] == settings.web_search_detector_labels


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("no route"), OSError("socket gone"), ValueError("garbled labels")],
)
def test_a_detector_that_cannot_be_reached_refuses_rather_than_waves_through(
    failure: Exception,
) -> None:
    gate = ScriptedGate(error=failure)

    assert dbutil.run(sanitizer(gate).refuses("a public question")) is Refusal.sanitizer_refused


def test_the_detector_labels_cover_private_context_and_personal_information() -> None:
    labels = set(settings.web_search_detector_labels)

    assert {"person name", "organization or team name", "private identifier"} <= labels
    assert {"software project or codebase name", "server or machine hostname"} <= labels
    assert {"email address", "phone number"} <= labels
