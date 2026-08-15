# crAIZK Devpost submission

Copy each section into the matching Devpost field. Replace only the marked credential and video
placeholders. Keep the testing credentials inside the private testing field.

## Project name

crAIZK

## Elevator pitch

Durable agent memory with source evidence, change history, and database-enforced access.

## Project story

### Inspiration

Most agent memory is a vector index filled with detached text. It can find a similar paragraph, but
it cannot always explain who wrote it, who may read it, or whether it is still true. That becomes a
serious problem when several people and agents share one memory.

We built crAIZK to make memory inspectable. A useful memory should preserve the original source,
record changes instead of overwriting them, and apply the same access boundary to keyword, vector,
graph, and temporal retrieval.

### What crAIZK does

crAIZK gives MCP agents five memory tools. `keep` stores a note, public source, or bounded file.
`find` retrieves evidence even when the question uses different wording. `status` reports identity,
usage, and processing health. `share` moves approved memories into team scopes. `report` records a
memory problem for the operator.

Every result labels its provenance. A source excerpt carries its document handle. Derived graph
knowledge is identified separately. A privacy receipt states whether the public web was consulted.
Row level security keeps personal memory private and allows controlled sharing with Logto
organizations.

The demonstration starts when an agent stores a short Project Atlas release policy. After durable
background processing finishes, the agent asks a differently worded question. crAIZK returns the
exact source and the answer without sending the private question to the web.

### How we built it

CockroachDB Cloud is the system of record. It stores source revisions, chunks, embeddings, temporal
facts, graph claims, authorization scopes, usage counters, and the durable work queue. C-SPANN
Distributed Vector Indexing searches a private scope-keyed vector projection. The result is then
hydrated from ordinary row-secured tables, so vector speed does not bypass authorization.

One AWS Lambda serves the website, documentation, browser interface, HTTP API, and modern MCP
endpoint through a Lambda Function URL. A second Lambda drains the CockroachDB queue, converts
files, embeds chunks, and extracts graph claims. Amazon S3 preserves private original files.
EventBridge Scheduler wakes recovery work, ECR stores immutable images, SSM Parameter Store holds
secrets, and CloudWatch provides short-retention operational evidence.

Logto provides OAuth and organization membership. The public MCP clients use PKCE and hold no
shared secret. OpenRouter routes the bounded demonstration workload to hosted extraction and
embedding models.

### Challenges we ran into

The hardest problem was not nearest-neighbor search. A direct scoped C-SPANN query took only a few
milliseconds, but an ordinary vector filter combined with row level security could force a scan.
The full recall statement then spent seconds evaluating parent visibility and composing graph
evidence.

We solved the candidate-search problem with a private vector projection keyed by vector kind and
exact scope. A capability function validates the requested scope against transaction authority
before issuing the C-SPANN query. Composite parent and scope keys let child policies authorize an
indexed scope without repeating a parent lookup for every row.

Lambda exposed a separate lifecycle bug. An async database pool was reused across different event
loops in warm worker invocations. The worker now keeps one event loop for the life of the Lambda
environment, while each function keeps one bounded database connection.

OAuth also required careful testing. The stable AWS URL, Logto resource, callback address, and MCP
client had to agree exactly. We replaced an embedded proxy with direct Logto token verification and
tested the flow from isolated Codex and OpenCode containers.

### Accomplishments that we're proud of

- The live scoped C-SPANN plan selected the distributed vector index and executed in 7 milliseconds.
- The bounded six-note cloud workload produced 40 facts, 19 entities, and four communities with no
  retained queue failure.
- Warm end-to-end recall measured a 2.14 second median and a 3.16 second p95 on the recorded cloud
  workload.
- The same workload answered all 29 expected points after semantic review while preserving the
  unchanged 26 of 29 exact-phrase score.
- CloudWatch reported no Lambda errors or throttles during the measured run.
- Direct Logto OAuth, all five MCP tools, private S3 upload, worker extraction, and grounded recall
  work from one public AWS URL.
- The public repository includes the complete infrastructure, migrations, documentation, bounded
  corpus, redacted query plans, and machine-readable result files.

These demonstration measurements describe the bounded six-note public corpus. They do not establish
a production service level.

### What we learned

Persistent memory is a database problem before it is a model problem. Vectors can find related
text, but useful memory also needs provenance, authorization, time, correction, and durable work
state.

We also learned that a fast vector index does not guarantee a fast memory request. Authorization,
connection setup, graph composition, and model calls must appear in the same profile as the index.
Measuring only nearest-neighbor search would have hidden the real bottleneck.

Finally, serverless simplicity comes from strict boundaries. The public Lambda cannot migrate the
database. The application role cannot bypass row security or read the private vector projection.
The worker receives bounded capabilities and every external service has a narrow purpose.

### What's next for crAIZK

The next database pass will reduce the cost of the composed CockroachDB recall statement while
preserving the source, community, entity, graph, and authorization behavior demonstrated here. We
also want to contribute the reusable PostgreSQL row security DDL building blocks upstream to
SQLAlchemy and keep the CockroachDB integration as small as possible.

A public production deployment would add a fail-closed malware scanner or quarantine boundary for
uploads. Japanese documents need a separate layout-aware OCR pipeline because multi-column page
order matters more than character tuning. Visual embeddings remain follow-up work. Broader
benchmarks and larger public corpora also need their own measured studies.

## Built with

- CockroachDB Cloud
- CockroachDB Distributed Vector Indexing
- ccloud CLI
- AWS Lambda
- Amazon S3
- EventBridge Scheduler
- Amazon ECR
- AWS Systems Manager Parameter Store
- Amazon CloudWatch
- AWS Budgets
- Model Context Protocol
- Python
- SQLAlchemy
- SQLModel
- FastMCP
- FastAPI
- Logto
- OpenRouter
- Qwen3 Embedding
- DeepSeek

Recommended Devpost tags

`cockroachdb`, `aws`, `lambda`, `s3`, `mcp`, `agentic-memory`, `vector-search`, `python`,
`sqlalchemy`, `serverless`

## Try it out

- Functional demo at `https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/`
- Machine-readable setup at `https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/setup.md`
- Documentation at `https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/docs/`
- Public repository at `https://github.com/phvv-me/aizk`
- Judge rehearsal at `https://github.com/phvv-me/aizk/blob/main/hackathon/DEMO.md`
- Architecture at `https://github.com/phvv-me/aizk/blob/main/hackathon/ARCHITECTURE.md`
- Cloud evidence at `https://github.com/phvv-me/aizk/blob/main/hackathon/PERFORMANCE.md`
- Public video at `VIDEO_URL_AFTER_UPLOAD`

## Functional demo application

`https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/`

## Testing credentials and instructions

Paste the credentials from the secure crAIZK demo directory into the private Devpost field.

```text
Account label
Maya Chen

Username
PASTE_DEMO_USERNAME_HERE

Password
PASTE_DEMO_PASSWORD_HERE
```

Do not place that credential block in the public project story or repository.

Use this test flow.

1. Open the functional demo and choose the application.
2. Sign in with the supplied Maya Chen account.
3. Confirm the dashboard shows the demonstration identity and an idle processing queue.
4. Open the setup link above and give its URL to a compatible MCP agent. The page provides the
   endpoint and client instructions.
5. Ask the agent to call `status`.
6. Ask it to keep the following note.

```text
# Project Atlas release policy

Project Atlas deploys one immutable artifact to staging and production. Its release gate checks p95
latency and rollback rate before promotion.
```

7. Wait until `status` reports idle processing.
8. Ask the following question with web access disabled.

```text
What should Atlas verify before promoting a release? Search only memory and keep web off.
```

The expected result names p95 latency and rollback rate. It includes the Project Atlas source
excerpt and document handle. Its privacy receipt states that nothing left the machine.

Exact Codex and pinned OpenCode rehearsal commands are available in
`https://github.com/phvv-me/aizk/blob/main/hackathon/DEMO.md`.

## Public repository

`https://github.com/phvv-me/aizk`

## Open source license

`https://github.com/phvv-me/aizk/blob/main/LICENSE`

The repository uses the canonical Apache License 2.0 text. The README links the license directly.

## CockroachDB tools used

Select these two options.

- CockroachDB Distributed Vector Indexing
- ccloud CLI

Do not select Managed MCP Server or Agent Skills Repo for the final entry.

## AWS services used

Select these options.

- AWS Lambda
- Amazon S3

## Meaningful CockroachDB and AWS integration

CockroachDB is the complete persistent memory layer rather than a side database. Each `keep` writes
the source revision and durable processing state into CockroachDB. The worker adds chunks,
embeddings, temporal facts, graph claims, and queue progress in the same transactional system.
Every `find` runs authorized lexical, vector, graph, and temporal retrieval against that state.
C-SPANN Distributed Vector Indexing searches the scope-keyed vector projection, while row level
security controls the source and derived rows returned to the caller.

The ccloud CLI was used to inspect cluster health, manage the restricted SQL identities, verify the
CockroachDB version and migration state, and capture redacted operational evidence. Its structured
output made those checks reproducible without exposing a connection string.

AWS Lambda runs both the public MCP application and the private worker. The MCP Lambda commits each
accepted memory operation to CockroachDB, then wakes the worker. The worker converts and enriches
the memory before it records every durable stage back in CockroachDB. Amazon S3 stores the original
bytes for bounded authenticated uploads, while CockroachDB stores their content identity,
provenance, scope, and processing state. The demonstration stops working if CockroachDB, Lambda, or
S3 is removed.

## Project start date

`07-03-26`

The first public AIZK commit is dated July 3, 2026. The CockroachDB and AWS profile begins in the
public history on July 23, 2026. Both fall inside the event's submission window.

## Pre-existing code or work

AIZK itself began during the event's submission window. crAIZK is the CockroachDB and AWS deployment of
that project, not a separately renamed earlier product.

The project uses four general-purpose house packages that existed before the event. `chefe` manages
reproducible environments and tasks. `patos` provides typed models and SQL model primitives.
`rlsalchemy` generates and audits row level security policies. `mainboard` provides hardware
inspection and profiling. These packages are independently versioned dependencies and are not
counted as hackathon features.

AIZK also builds on the open-source and hosted dependencies listed in
`https://github.com/phvv-me/aizk/blob/main/hackathon/DISCLOSURE.md`. OpenAI Codex and Claude Code
were used as development assistants. The public commit history, tests, query plans, and benchmark
artifacts remain the evidence for the submitted work.

## Architectural diagram

Upload `hackathon/media/00-architecture.png`.

The source explanation is available at
`https://github.com/phvv-me/aizk/blob/main/hackathon/ARCHITECTURE.md`.

## Optional CockroachDB feedback

Distributed Vector Indexing was impressively fast once the query reached an exact scope-keyed
C-SPANN span. The difficult part was keeping that plan when tenant filtering and row level security
were also present. A documented reference pattern for filtered or tenant-scoped C-SPANN queries
would be valuable, especially when the filter is derived from database authorization rather than
trusted application input.

The ccloud CLI worked well for agent-driven operations because its noun and verb structure is
predictable and its JSON output is easy to redact and verify. Managed MCP was useful for read-only
inspection during development, but frequent interactive reauthorization made it less suitable for
repeatable automated checks. Longer-lived device authorization or service-account support would
make it a stronger operational tool for agents.

## Submitter information

### Submitter type

Individual

### Country of residence

Japan

### Organization name

Not applicable

## AI tools used

- OpenAI Codex for development, review, testing, and isolated agent rehearsals
- Claude Code for development and review
- DeepSeek through OpenRouter for bounded extraction
- Qwen3 Embedding 8B through DeepInfra for text embeddings

## Level of learning

High. The project required hands-on work across distributed vector indexing, row level security,
MCP OAuth, serverless lifecycle behavior, durable queues, query planning, observability, and
evidence-led performance tuning. The largest lesson came from profiling the complete authorized
memory request rather than treating a fast vector probe as proof that the application was fast.

## Career value

Yes. The work produced reusable patterns for tenant-safe vector search, least-privileged serverless
database access, direct MCP OAuth, durable background processing, and performance claims grounded
in query plans and operational evidence.

## Required owner attestations

The project owner must review and select each statement in Devpost.

- The submitter and any teammates are not employees of the sponsor, its affiliates, or a government
  entity.
- The submitter and any teammates are from an eligible jurisdiction.
- The submitter and any teammates are at least the age of majority where they reside.

## Project media

Upload the following files from `hackathon/media`. Every gallery image uses a 3 to 2 aspect ratio
and remains below the 5 MB image limit. The separate architecture upload keeps its wider layout for
readability and remains below its 35 MB limit.

Use `thumbnail-devpost.png` as the project thumbnail. It is a dedicated 3 to 2 composition with
the crAIZK name and short product promise.

1. `00-hero.jpg` with the AIZK identity and product promise
2. `01-one-action-onboarding.jpg` with the deployed landing page
3. `02-agent-setup-guide.jpg` with the machine-readable setup page
4. `03-agent-configures-aizk.jpg` with the clean Codex setup
5. `05-authenticated-status.jpg` with the Maya identity and MCP status
6. `06-live-memory-write.jpg` with the Project Atlas write
7. `07-grounded-recall.jpg` with the source excerpt and privacy receipt
8. `08-memory-console.jpg` with the authenticated evidence browser
9. `10-cspann-plan.jpg` with the redacted CockroachDB index plan
10. `11-lambda-operations.jpg` with the redacted Lambda evidence
11. `00-architecture.png` as the optional architecture upload

The image captions are recorded in `hackathon/media/README.md`.
