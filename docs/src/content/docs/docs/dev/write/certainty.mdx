---
title: "Settledness and contradiction"
description: "Keeping the certainty a source expressed, and closing a claim a later document disproves."
---

This page covers the axis that says how settled a claim is, the deterministic check that keeps a
fact from stating more than its own sentence did, and the path by which a later document retracts an
earlier claim. It follows [Grounding and consolidation](/docs/dev/write/consolidation/), which owns
the rest of the audit and the cascade.

## Two independent axes

`EpistemicKind` answers whose claim this is. `Stance` answers how settled it is. Folding them
together would make every combination a new enum member, so they stay apart and a claim carries
both.

```text
  settled  ->  reported  ->  hedged  ->  disputed  ->  refuted
  plain        credited      qualified   sources      the source
  assertion    to somebody   or doubted  disagree     says it is wrong
```

The ladder only ever runs downward. `at_least` takes the less settled of two readings, because
evidence about a claim may lower confidence in it and never raise it. Two properties do the work
elsewhere. `distorting`, true from `hedged` down, decides whether reading a claim flatly would
change what it says. `decisive`, true for `settled` and `refuted`, decides whether a claim is
definite enough to close one it contradicts.

Only an unsettled stance is stored, in the claim's `attributes` beside the speaker and the
epistemic kind, so nothing is written for the ordinary case and no schema changed.

## Why a quote check was not enough

A hedged sentence contains a contiguous, character-exact substring that asserts its confident half
alone.

```text
  source    RIRobustNetV7 improves over RIConv++ on noise, though the
            margin falls within run-to-run variance.
  quote     RIRobustNetV7 improves over RIConv++ on noise
  fact      RIRobustNetV7 beats RIConv++ on noise and occlusion.
```

Every check that existed passed. The quote is verbatim, contiguous and locatable, and the stored
fact says the opposite of what the author wrote. The system behaved as specified and the
specification was wrong for hedged content.

## Certainty is a comparison

`Qualification.read` expands the located quote to the sentence around it and reads both that
sentence and the fact's own statement through one certainty detector, then acts on the difference.

The statement is the compared side rather than the quote, because the statement is what gets stored,
embedded, ranked and handed to a reader. A quote spanning a qualifier the statement then drops still
leaves a flat assertion in the graph. The detector maps four marker registers onto `Stance`, and its
lexicon is a cheap detector rather than the decision, since a register it does not know is missed
symmetrically on both sides and costs an error in neither direction.

Two differences reject the fact as `stripped_qualifier`. A statement less doubtful than a sentence
that is itself at `hedged` or worse, and a negation in the sentence the statement drops. A dropped
attribution does not reject, because the claim survives intact and the stance plus the paired source
excerpt carry what was lost. Whatever survives is stamped through `settledness`, which may only
lower what the model proposed.

## What the reader gets

`LiveFact.line` renders the marks in one bracket, and a settled world claim with no speaker prints
none.

```text
  - [hedged] (improves_over) V7 improves over the baseline on noise
  - [Maya, opinion, disputed] (uses) the batch lane is the wrong default
```

The label is not the whole defense. Published 2026 work finds the evidential register barely
discounted by downstream readers, so an unsettled claim also changes what recall returns.
`deduplicate` stops treating a source excerpt as a repetition of an unsettled fact from the same
span, which is what makes the source-wins rule in
[Sources and derived knowledge](/docs/user/concepts/sources/) a property of the answer rather than
advice to the reader. The rendered result also opens with a standing instruction naming the excerpt
as the authority.

## Closing a claim a later document disproves

A source replaced in place already closes everything built from its old text. A later, different
document showing an earlier claim was wrong had nothing to close it, so the claim stayed live and
kept answering questions.

`TimedFact.contests` routes that case straight to the batched model call, skipping the
deterministic tiers, because a correction reads almost exactly like the claim it corrects and
similarity would call it a near-duplicate. It is true when the supporting sentence announces a
correction or when extraction read the source as disputing or refuting the claim.

The model may then answer `REFUTE`, naming a claim from that candidate's own catalog and no other.
`GraphWriter._settle_contradictions` applies it conservatively.

| The new fact's stance | What happens to the claim it contradicts |
|---|---|
| `settled` or `refuted` | closed through `Fact.Claim.refute`, stamped with the chunk that closed it |
| anything less certain | left live and marked `disputed`, and the new claim joins the standoff |

A false retraction silently deletes something a person committed to memory while an unresolved
disagreement is merely visible, so the asymmetry is deliberate.

This is also the correction path a caller has. There is no tool that edits a derived claim, because
derived claims are read out of sources and any hand edit would be overwritten. A `keep` note saying
plainly that a claim is refuted goes through this same path and closes it, with the note recorded as
the evidence that closed it.

## Measured, not asserted

`chefe run aizk-eval certainty` scores this on a committed corpus where every quote is a contiguous,
character-exact substring, so each case already passes quote verification. The before arm is the
same audit without the comparison, which is what the code did when the defect was reported.

| Measure | Before | After |
|---|---|---|
| flattening admitted, over 12 distorting proposals | 1.000 | 0.083 |
| false rejection, over 20 faithful proposals | 0.000 | 0.050 |
| source excerpt paired with an unsettled claim | 0.000 | 1.000 |
| settledness read matches the human label | | 0.969 |

[Extraction and models](/docs/dev/eval/extraction/) records the lexicon ablation behind those
numbers and reads the two remaining costs.

## Next

<div class="not-content">

- [Grounding and consolidation](/docs/dev/write/consolidation/) owns the rest of the audit.
- [The bi-temporal model](/docs/dev/store/bitemporal/) explains the ranges a refutation closes.
- [Evidence and provenance](/docs/user/concepts/evidence/) is the reader-facing view.

</div>
