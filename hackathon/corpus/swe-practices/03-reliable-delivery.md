# Reliable delivery and recovery

- Type Concept
- part_of [Concept] Software engineering practice map
- depends_on [Concept] Testing and code review
- uses [Pattern] Expand and contract

Deployment must be automated, repeatable, and verified. The release record should link source,
resolved dependencies, build, artifact, change, and deployed identity. Checking that every target
runs the same artifact catches partial rollouts and configuration drift before they become long
incidents.

Safe delivery separates deployment from release when risk justifies it. Canarying, feature flags,
and progressive rollout reduce the blast radius. Every migration and rollout needs an explicit
recovery path. Timeouts bound waiting. Retries use backoff and jitter, remain bounded, and operate
only on idempotent work. Restore readiness must be demonstrated by exercises that verify integrity
and meet recovery objectives. A backup that has never been restored is not recovery evidence.

Database changes use expand and contract. Add the compatible structure first, deploy code that can
work through the transition, backfill in bounded batches, validate the result, switch reads, and
remove the old structure only after rollback is no longer needed. Migration safety includes
compatibility, data movement, validation, observability, and recovery.

Public references

- [Google SRE guidance for canarying releases](https://sre.google/workbook/canarying-releases/)
- [AWS guidance for timeouts, retries, and jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
- [Martin Fowler on parallel change](https://martinfowler.com/bliki/ParallelChange.html)
- [SLSA build provenance](https://slsa.dev/spec/v1.2/provenance)
