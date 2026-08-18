# Official rules and compliance

Checked on August 13, 2026 against the
[official Devpost rules](https://cockroachdb-ai.devpost.com/rules) and
[event page](https://cockroachdb-ai.devpost.com/).

The submission period runs from June 30, 2026 at 10 AM EDT through August 18, 2026 at 5 PM EDT.
The deadline is August 19 at 6 AM JST. The repository and working project must remain available
through the end of judging.

## Required build

The entry must be a newly created agentic application that uses CockroachDB as its persistent
memory layer and runs on AWS. Each integration must do real work. The project must use at
least two approved CockroachDB tools and at least one AWS service.

Approved CockroachDB tools include CockroachDB Cloud Managed MCP, Distributed Vector Indexing,
the ccloud CLI, and the CockroachDB Agent Skills repository. Approved AWS services include Lambda,
Bedrock, ECS, EKS, S3, SageMaker, and other services that power the agent environment.

## Current compliance

| Requirement | Current evidence | Status |
|---|---|---|
| Entrant eligibility | Japan is not listed among excluded territories. The entrant must confirm age and Devpost account eligibility before submission. | Manual confirmation pending |
| New project during the submission period | The first AIZK commit is dated July 3, 2026. The CockroachDB and AWS profile begins on July 23. | Ready |
| Public repository | `https://github.com/phvv-me/aizk` is public. | Ready |
| Open source license | GitHub recognizes the committed Apache 2.0 license. | Ready |
| Agentic application | The modern MCP server exposes `status`, `find`, `keep`, `report`, and `share` with direct Logto verification. | Ready |
| CockroachDB persistent memory | Documents, graph claims, temporal state, scopes, vectors, usage, and the durable queue use CockroachDB. | Cloud smoke passed |
| Distributed Vector Indexing | C-SPANN powers the private scoped vector projection and the live cloud plan selected it in 7 milliseconds. | Ready in cloud |
| ccloud CLI | The pinned CLI authenticates, inspects the cluster, and manages SQL users. | Ready, final deployment transcript pending |
| Managed MCP | A fixed collector uses the official server for live cluster and bounded queue evidence through `get_cluster` and `select_query`. DeepSeek receives the normalized snapshot and no database tools. | Live service-key inspection passed |
| Agent Skills | The queue steward loads the official cluster health and background job skills before it classifies evidence or recommends recovery. | Live skill-guided verdict passed |
| AWS service | Two deployed Lambda functions serve the product and queue work. Private S3 stores original artifacts. EventBridge Scheduler wakes recovery work. | Cloud smoke passed |
| Meaningful integration | The Logto protected Function URL completed modern discovery, all five tool discovery, private S3 upload, extraction, find, and identity resolution against CockroachDB Cloud. | Cloud smoke passed |
| Working demo URL | The stable AWS Function URL serves the site, docs, browser UI, API, and MCP to invited Logto users. The public Native Codex client has no secret and reaches an accepted Logto sign-in request. | Ready for interactive rehearsal |
| Public example material | Six curated public SWE reference notes exercise source, graph, community, and multihop find in CockroachDB Cloud. They are the frozen demo corpus pending a final reference review. | Partial |
| Setup and run instructions | Product setup and local Lambda rehearsal are documented. A clean external rehearsal remains. | Partial |
| Pre-existing work disclosure | [DISCLOSURE.md](DISCLOSURE.md) records shared code and development dates. | Ready |
| Authorized third-party use | Dependencies, hosted services, and public data sources are identified in the disclosure. Final corpus terms still need review. | Partial |
| English video under three minutes | [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) targets two minutes and forty-five seconds. | Pending recording |
| Video shows the project and memory layer working | The shot plan includes MCP writes, C-SPANN find, the CockroachDB console, and Lambda. | Pending recording |

The queue steward makes Managed MCP and the CockroachDB Agent Skills part of the operator workflow.
Its service account is limited to Cluster Operator on the demonstration cluster. The runner makes
only fixed read-only MCP calls and one fixed `SHOW JOBS` aggregate through a SQL login with only
`VIEWJOB`. It sends only normalized operational evidence to DeepSeek and requires explicit
approval before an AIZK domain command may retry work. The model has no database tools and cannot
choose SQL.

The source and reproducible invocation are committed under `operator`. The gallery contains only
redacted product, database, and AWS evidence. Credentials, cluster identifiers, organization
values, source text, queue payloads, and stored errors are excluded.

## Submission deliverables

- A public source repository with all source, dependencies, example configuration, setup, and run
  instructions
- A working project URL that judges can test
- A written description of features and functionality
- The CockroachDB tools used and how each one matters
- The AWS services used and how each one matters
- An English demonstration video hosted on YouTube or Vimeo and shorter than three minutes
- Footage showing both the application and the CockroachDB memory layer working
- An optional architecture diagram
- Honest disclosure of incorporated pre-existing code or work

## Judging criteria

The five criteria have equal weight.

| Criterion | Question the submission must answer |
|---|---|
| Agentic Memory Design | Does CockroachDB have a substantial production role as persistent agent memory? |
| Technological Implementation | Is the CockroachDB integration sound software engineering? |
| Real-World Impact | Could this materially improve a real user or workflow? |
| Product Readiness | Is it secure and observable, and can it grow safely? |
| Creativity and Originality | Is the idea or application actually novel? |

## Release gate

- [x] Deploy one clean CockroachDB Cloud database from the current baseline
- [x] Deploy the Lambda image and invoke setup successfully
- [x] Load the bounded public SWE rehearsal corpus through the deployed MCP endpoint
- [x] Confirm extraction drains without retained queue failures
- [x] Capture cold and warm find latency from AWS
- [x] Capture a redacted C-SPANN query plan from CockroachDB Cloud
- [x] Run `status`, `keep`, and `find` as the judge-visible Maya identity
- [ ] Verify the public demo without a local credential or private network
- [ ] Rehearse setup from a clean clone using only committed instructions
- [x] Record and inspect gallery images with no secret, private note, or unrelated deployment visible
- [ ] Record and publish the English video under three minutes
- [ ] Open the video and demo from a signed-out browser
- [ ] Complete every Devpost field and submit before August 19 at 6 AM JST
