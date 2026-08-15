# Software engineering practice map

- Type Concept
- uses [Concept] Maintainable design and modular boundaries
- uses [Concept] Testing and code review
- uses [Concept] Reliable delivery and recovery
- uses [Concept] Observability and production diagnosis
- uses [Concept] Evidence-led performance engineering

Good software engineering is a connected control system rather than a checklist. Maintainable
boundaries limit the amount of code affected by a change. Focused tests verify behavior at those
boundaries. Independent review evaluates context that an automated rule cannot see. Continuous
integration turns both into fast feedback on a reproducible artifact.

Reliable delivery carries that artifact into production through a bounded, observable rollout.
Deployment identity connects failures back to source and build evidence. Observability shows what
users experienced and where the system spent time. Recovery exercises verify that operators can
restore service. Production evidence then informs the next design and performance decision.

Some practices are mechanically enforceable. Examples include dependency direction, required CI
gates, migration ordering, bounded retries, release provenance, and whether telemetry carries a
deployment identifier. Other questions require contextual judgment. Examples include whether an
abstraction is helpful, whether a benchmark is representative, whether a test strategy matches
risk, and whether an optimization earns its complexity. A useful engineering system distinguishes
these two kinds of decisions instead of converting every judgment into a numeric rule.

This map synthesizes the public references attached to the five supporting notes in this corpus.
