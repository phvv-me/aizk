# crAIZK submission workspace

The submission thumbnail is generated from the canonical aizk social card at `thumbnail.png`. Run
`chefe run aizk-brand` from the monorepo root after changing any brand source.

This directory contains event-specific material for the CockroachDB and AWS Build with Agentic
Memory Hackathon. The product README and documentation remain useful without this directory.

The submission closes on August 18, 2026 at 5 PM EDT, which is August 19 at 6 AM JST.

## Contents

- [RULES.md](RULES.md) records the official requirements and current evidence for each one.
- [PLAN.md](PLAN.md) owns the remaining checklist, daily timeline, gates, and manual decisions.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains the submitted CockroachDB and AWS design.
- [DISCLOSURE.md](DISCLOSURE.md) identifies project dates and every category of pre-existing work.
- [DEMO.md](DEMO.md) defines the clean judge flow and the local Lambda rehearsal.
- [SUBMISSION.md](SUBMISSION.md) holds the Devpost narrative and form answers.
- [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) holds the two minute and thirty second recording plan.
- [`workload.py`](workload.py) loads the public corpus and measures modern MCP through Lambda.
- [`codex`](codex) contains the isolated Codex Luna judge-client rehearsal.
- [`results`](results) contains dated, machine-readable benchmark and smoke-test evidence.
- [`screenshots`](screenshots) contains only final submission captures and its shot list.

## Current state

The repository, license, build dates, local Lambda simulation, CockroachDB schema, C-SPANN search,
managed AWS deployment, public demo URL, S3 flow, Logto identity, and isolated Codex Luna client have
been verified. Final screenshots and video are still pending. [PLAN.md](PLAN.md) is the working
execution board, while [RULES.md](RULES.md) remains the compliance gate.

## Separation rule

Anything that exists only because of this event belongs here. Runtime, deployment, security, and
operator documentation stays beside the product code when it remains useful after judging.
