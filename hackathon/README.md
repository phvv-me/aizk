# AIZK hackathon submission

This directory contains only the material needed to submit and judge AIZK for the CockroachDB and
AWS Build with Agentic Memory Hackathon. Product documentation and deployment instructions remain
with the product code.

## Submission files

- [SUBMISSION.md](SUBMISSION.md) contains the Devpost narrative and form answers.
- [DEMO.md](DEMO.md) gives judges one short reproducible product flow.
- [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) contains the final recording script.
- [RULES.md](RULES.md) maps the entry to the event requirements.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains the CockroachDB and AWS design.
- [DISCLOSURE.md](DISCLOSURE.md) identifies incorporated work and third-party sources.
- [media](media) contains the thumbnail, gallery images, and architecture diagram.

## Reproducible evidence

- [`workload.py`](workload.py) loads the bounded public corpus and exercises the deployed MCP API.
- [`corpus`](corpus) contains the public example data and its source references.
- [`operator`](operator) contains the read-only queue steward that uses CockroachDB Managed MCP and
  the official CockroachDB Agent Skills.

Generated recordings, dated result dumps, client rehearsal containers, and duplicate screenshots
are intentionally excluded.
