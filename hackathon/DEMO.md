# Judge demo

This flow demonstrates one complete memory operation through the deployed AIZK service. It needs
no repository checkout. Testing credentials are provided privately in Devpost.

## Open AIZK

Use the functional demo application.

```text
https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/
```

The landing page provides short setup commands for Claude Code and Codex. Complete Logto sign-in
with the supplied fictional account, then ask the client to call `status`.

## Keep one decision

Ask the agent to keep this public demonstration note.

```text
# Project Atlas release policy

Project Atlas deploys one immutable artifact to staging and production. Its release gate checks p95
latency and rollback rate before promotion.
```

Call `status` until processing is idle. The write, source revision, processing state, and derived
knowledge remain durable in CockroachDB while the private worker Lambda completes enrichment.

## Find the source

Ask a differently worded question with public web access disabled.

```text
What should Atlas verify before promoting a release? Search only memory and keep web off.
```

The expected answer names p95 latency and rollback rate. It includes the Project Atlas excerpt and
document handle. Its privacy receipt confirms that the public web was not used.

## Inspect the product

Open the browser application and verify the same source, processing state, organization scope, and
usage activity. The dashboard and agent are two views over the same CockroachDB state.

## Inspect the operational agent

The read-only queue steward provides the second agentic CockroachDB integration. Configure its
cluster-scoped service account as described in [`operator/README.md`](operator/README.md), then run
one diagnosis from the repository root.

```sh
uv run --no-sync python hackathon/operator/steward.py diagnose
```

The verdict names the Managed MCP tools and official Agent Skills that supplied its evidence. The
model receives normalized operational counts and no write-capable database tool. Any repair still
requires explicit operator approval.

## Local reproduction

The root README covers local startup. The AWS deployment guide under `infra/aws` covers the Lambda,
S3, CockroachDB Cloud, and Logto configuration. The bounded workload can load either corpus and run
the same MCP checks against a local or deployed endpoint.
