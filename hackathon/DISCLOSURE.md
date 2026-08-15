# Pre-existing work and development disclosure

This disclosure is intentionally broader than the minimum required by the official rules.

## Project dates

AIZK began on July 3, 2026, after the submission period opened on June 30. The first committed
CockroachDB and AWS deployment profile is dated July 23. Both dates are visible in the public Git
history.

```sh
git log --reverse --format='%ad %h %s' --date=iso-strict
git log --all --regexp-ignore-case --grep='cockroach\|c-spann\|ccloud\|lambda\|aws'
```

crAIZK is not presented as a separate codebase invented after AIZK. It is the CockroachDB and AWS
deployment of the same new project. The PostgreSQL deployment, browser, artifact pipeline,
retrieval engine, temporal graph, and authorization model are shared product work created during
the same submission period.

## Pre-existing house packages

AIZK depends on general-purpose packages that existed before this event.

- `chefe` manages reproducible environments and tasks.
- `patos` provides typed models and SQL model primitives.
- `rlsalchemy` generates and audits row level security policies.
- `mainboard` provides hardware inspection and profiling.

These packages are independently published, versioned dependencies. They were not created for the
submission and are not counted as hackathon features.

## Third-party software and services

The project builds on open-source Python, PostgreSQL, CockroachDB, SQLAlchemy, SQLModel, FastMCP,
FastAPI, Alembic, pgvector, PgQueuer, Docling, ClamAV, SeaweedFS, Logto, Astro, SvelteKit, and their
transitive dependencies. The cloud path uses CockroachDB Cloud, AWS, and OpenRouter under their
respective service terms.

No third-party project is represented as original AIZK work. Apache 2.0 covers the AIZK source,
while dependencies retain their own licenses.

## Data

The cloud demonstration corpus contains the six authored synthesis notes under
`hackathon/corpus/swe-practices`. They cover maintainable design, testing and review, reliable
delivery, observability, evidence-led performance, and their shared practice map. The notes link to
the public references that informed them. They do not preserve or reproduce the referenced books,
papers, or websites as uploaded artifacts. The recorded demo adds one short original Project Atlas
note solely to show a live write and differently worded recall.

Private production memory, private scans, and third-party PDFs are excluded from
crAIZK. The larger 87-document AIZK documentation corpus remains local reproducibility evidence and
is not part of the final cloud demo corpus.

Production AIZK retains its direct visual embedding interface. crAIZK does not call it. The demo
uses bounded figure descriptions and embeds the resulting text so image handling remains affordable
and compatible with the selected hosted endpoint.

[The public corpus reference review](REFERENCES.md) lists all nineteen outbound citations, the
engineering terms they support, and the automated availability check performed on August 13.

## AI assistance

OpenAI Codex and Claude Code were used as development assistants. Hosted models provide runtime
embedding, extraction, and optional answer support. Human review, tests, database plans, benchmark
artifacts, and the public commit history remain the evidence for submitted claims.
