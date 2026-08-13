# crAIZK Devpost draft

Copy-ready material for the CockroachDB and AWS Build with Agentic Memory Hackathon. Replace no
placeholder until the linked deployment evidence exists.

## Elevator pitch

Give every agent a durable memory that keeps its sources and respects who may read them.

## Inspiration

Agent memory often means a vector database full of detached text. It can find a similar paragraph
without explaining who may read it or when it was true. It also loses corrections and the original
document. We built AIZK because people and agents need memory they
can question rather than a cache they must trust.

## The product

crAIZK is the cloud profile of the AIZK AI Zettelkasten, deployed with CockroachDB Cloud and AWS
Lambda. An MCP agent
can keep authored notes and public sources, then retrieve grounded evidence later with different
wording. Background work extracts entities and temporal facts while preserving the source document.

CockroachDB holds documents, chunks, vectors, graph claims, validity time, transaction history,
authorization scopes, usage counters, and the durable work queue. Row level security enforces the
same scope boundary for lexical, vector, graph, and temporal retrieval. The answer contains labelled
source excerpts and document handles, so an agent can distinguish authored evidence from derived
memory and public web material.

## How we built it

The public interface is modern MCP `2026-07-28` with five tools named `status`, `find`, `keep`,
`report`, and `share`. One Lambda Function URL serves the site, documentation, Logto browser UI,
HTTP API and MCP. A separate Lambda drains the portable CockroachDB queue, converts files,
embeds chunks, and extracts graph claims. EventBridge wakes recovery work every fifteen minutes.
The worker accepts an explicit setup event for migrations. Private S3 preserves original uploads
while Logto and each public PKCE client own authorization state.

C-SPANN Distributed Vector Indexing powers a private scoped vector projection. Large vectors live
in their own column families, and composite parent and scope keys let row security use indexed child
scopes without repeating a parent lookup for every candidate. ccloud manages the cluster and SQL
users. CockroachDB Cloud Managed MCP gives the operator a guarded inspection surface without a
custom proxy.

## Challenges we ran into

The hardest performance problem was not vector search. Direct scoped C-SPANN took 3 to 7
milliseconds in [the dated cloud evidence](PERFORMANCE.md), while the composed graph plan took
seconds. EXPLAIN ANALYZE showed a parent visibility
function running once per child row. Enforcing parent and child scope identity with composite
foreign keys let reads use the child scope index directly and cut warm Lambda recall latency.

Lambda exposed a second problem. An asyncpg pool reused a connection across separate event loops in
warm worker invocations. The worker now keeps one event loop for the life of its execution
environment, while each Lambda keeps one bounded database connection. Consecutive batches finish
without connection errors. We measured startup and query embedding separately from database and
response work. Reusing one bounded warm connection reduced the final six-note cloud
workload to a 2.14 second warm median and 3.16 second warm p95 while preserving the same grounded
answer coverage in [the cloud workload result](results/craizk-swe-cloud-2026-08-12.json).

## Accomplishments that we are proud of

The local AWS simulation loaded 87 public documents as 345 chunks through real Lambda events in
[the dated local workload result](results/local-docker-2026-08-10.json).
Modern MCP writes and durable processing work end to end. The result includes scoped C-SPANN
evidence and status. The public Logto path completes PKCE authorization with all five tools. It
also completes a bounded S3 upload followed by worker extraction and grounded recall. The
repository gates formatting, imports, three type checkers, infrastructure synthesis, and deployment
tests.

## What we learned

Persistent memory is a database problem before it is a model problem. Vectors find related text,
but useful memory also needs provenance, authorization, time, correction, and durable work state.
Keeping those in one transactional system removes the gaps created by a separate vector store,
queue, and graph database.

We also learned that a fast index does not guarantee a fast memory query. Authorization and graph
composition must be present in the benchmark, because an isolated nearest-neighbor result can hide
the work the real agent request performs.

## What is next

The next product pass will speed up the composed CockroachDB graph and authorization query whose
cost is much larger than the direct C-SPANN lookup. A public deployment would also replace the
invited-demo scanner bypass with a fail-closed malware scanner or quarantine boundary. Japanese
document support remains a separate layout and conversion project rather than part of this demo.

## CockroachDB tools used

- Distributed Vector Indexing powers every embedded retrieval lane through C-SPANN.
- ccloud authenticates, inspects the cluster, and manages restricted SQL users.
- CockroachDB Cloud Managed MCP supports guarded operator inspection and query analysis.

## AWS services used

- Lambda runs the MCP and worker functions.
- Lambda Function URL exposes the MCP HTTP boundary.
- S3 preserves private original artifacts. Logto and each public PKCE client hold OAuth state.
- EventBridge Scheduler wakes durable recovery work.
- ECR stores the immutable application image.
- SSM Parameter Store holds database and model credentials.
- CloudWatch provides short-retention logs and AWS Budgets tracks gross monthly cost.

## Pre-existing work

AIZK began on July 3, 2026, and its CockroachDB and AWS profile began on July 23. Both dates fall
within the hackathon eligibility window. The project uses the general-purpose house packages chefe, patos,
rlsalchemy, and mainboard, plus the third-party software listed in
[DISCLOSURE.md](DISCLOSURE.md). OpenAI Codex and Claude Code were used as coding assistants.

## Try it

- Repository at `https://github.com/phvv-me/aizk`
- Public demo and documentation at `https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/`
- Logto protected MCP at `https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/mcp`
- Local judge rehearsal in [DEMO.md](DEMO.md)
- Architecture in [ARCHITECTURE.md](ARCHITECTURE.md)
- Cloud workload result in [`results/craizk-swe-cloud-2026-08-12.json`](results/craizk-swe-cloud-2026-08-12.json)
- Redacted C-SPANN plan in [`results/craizk-cspann-cloud-2026-08-12.txt`](results/craizk-cspann-cloud-2026-08-12.txt)
- Redacted cloud schema receipt in [`results/craizk-cloud-schema-2026-08-13.json`](results/craizk-cloud-schema-2026-08-13.json)
- Redacted AWS operations receipt in [`results/craizk-cloud-operations-2026-08-13.json`](results/craizk-cloud-operations-2026-08-13.json)
- Video URL will be added after the signed-out playback check
