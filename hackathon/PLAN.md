# AIZK execution plan

This is the working checklist for finishing and submitting AIZK. [RULES.md](RULES.md) remains
the compliance source. This file owns sequencing, dates, acceptance criteria, and the work that is
still open.

## Deadline clock

Checked on August 17, 2026 at 1:44 PM JST.

| Milestone | Time in Japan | State |
| --- | --- | --- |
| Current point | August 17 at 1:44 PM JST | 1 day and 4 hours remain before the internal cutoff |
| Internal submission cutoff | August 18 at 6 PM JST | 12 hour safety buffer |
| Official submission deadline | August 19 at 6 AM JST | Hard stop |
| Judging ends | September 16 at 6 AM JST | Keep everything working through this time |
| Expected winner announcement | Around September 22 at 4 AM JST | No action required |

The internal deadline is the real target. Nothing except an emergency repair should change after
August 18 at 6 PM JST.

## Top flag

The deployed product and public repository are healthy. GitHub Actions passes the complete package,
database, infrastructure, documentation, and dashboard gates from a standalone checkout. Both
marketplace manifests return 200, and an isolated Claude configuration installs and enables the
AIZK plugin. A signed-in dashboard pass in the isolated browser, final video editing, and the Devpost
entry remain open.

## Current readiness

| Area | State | Evidence or gap |
| --- | --- | --- |
| Public repository and license | Ready | GitHub is public, current, and reports Apache 2.0 |
| Local CockroachDB application | Ready | Local Lambda simulation loaded 87 documents as 345 chunks |
| Persistent memory design | Ready locally | Documents, vectors, graph, time, scopes, usage, and queue share one database |
| Distributed Vector Indexing | Ready in cloud | Live scoped C-SPANN selected the vector index and executed in 7 milliseconds |
| Modern MCP | Ready in cloud | `status`, `find`, `keep`, `report`, and `share` use protocol `2026-07-28` with direct Logto token verification |
| Automated tests | Ready | 1,767 tests pass at full coverage locally and in GitHub Actions |
| Formatting gate | Ready | Ruff lint and format checks pass across 529 files locally and in GitHub Actions |
| Type gate | Ready | Pyrefly, ty, and mypy pass |
| Infrastructure synthesis | Ready | CDK synthesis and ten deployment tests pass |
| Web gate | Ready | Svelte diagnostics, formatting, 24 tests, and the production build pass in GitHub Actions |
| Documentation and plugins | Ready | The 94-page build, privacy checks, setup consistency, plugin validation, and isolated Claude installation pass |
| CockroachDB Cloud cluster | Ready | The Singapore serverless database is migrated and restricted app access works |
| CockroachDB Managed MCP | Ready in cloud | Cluster-scoped service account inspection returned a healthy typed verdict through the read-only allowlist |
| CockroachDB Agent Skills | Ready in cloud | Official cluster health and background job skills drove the live queue steward verdict |
| AWS operator tooling | Ready | Pinned AWS CLI 2.36.20 authenticates through the isolated `craizk` profile in Singapore |
| AWS promotional credit | Ready | The current account has $100 unused through August 11, 2027 |
| AWS runtime | Ready | MCP and worker use `sha256:3280ad739f7021ee025432c33d68d4cbfcccd0cf93cdc3ed888ba087f8fb21fa`, while the web Lambda uses `sha256:00d28570ff61b6e985da81ad93aebdda39abbd17e0941594172eeeb6c6df8b50` |
| Judge website and UI | Ready for browser rehearsal | The assigned AWS URL serves the landing page, full docs, Logto sign-in, production dashboard and authenticated API |
| File intake | Ready for rehearsal | A 4 MiB file cap, format verification, 1 GiB user quota, private S3 and worker find passed live |
| Cloud corpus and benchmark | Ready for rehearsal | Six public notes produced 40 facts, 19 entities, four communities, 29 of 29 semantically complete answers, and a 2.14 second warm median |
| Judge identity and access | Ready for final rehearsal | Maya Chen completed direct OAuth and the live flow. Her secure SSM demo-directory entry is current |
| Screenshots and video | In progress | A privacy-reviewed nine minute walkthrough shows live onboarding, OAuth, write, processing, find, console, AWS and CockroachDB evidence. Final narration, short edit and captions remain |
| Devpost entry | Manual confirmation pending | Eligibility, account standing, draft, and final submit need the project owner |

## Remaining critical path

These are the only submission blockers, in order.

1. Verify the signed-in dashboard from clean client and browser profiles.
2. Cut the complete walkthrough to the narrated submission length, upload it, and verify it while
   signed out.
3. Create and complete the Devpost draft, run the final smoke twice, then submit by August 18 at
   6 PM JST.

The composed query plan, new benchmark automation, extra corpus material, budget email, and a
replacement thumbnail remain closed as out of scope.

## Scope freeze

The submission is the text-memory MCP path already proven locally. It is not a general AIZK release.

Include the following work.

- Authenticated modern MCP on an AWS assigned URL
- CockroachDB Cloud as the persistent memory, vector, graph, authorization, temporal, and queue store
- Lambda for MCP plus a private worker that also handles explicit setup events
- Lambda Function URL, EventBridge Scheduler, ECR, SSM, short CloudWatch logs, and one ten dollar gross-cost budget that excludes credits
- Distributed Vector Indexing for scoped semantic Find
- ccloud for reproducible cluster and SQL identity management
- Managed MCP for live read-only queue and database evidence
- Official Agent Skills for the queue steward diagnosis and safety rules
- One bounded public corpus and one complete grounded memory story
- One private S3 artifact path with a bounded authenticated upload and text-only embedding
- Honest cold and warm cloud measurements

Keep the following work out of the submission critical path.

- Japanese OCR repair and the reconversion sweep
- Production OCR tuning and later converter refinements
- Visual embedding and new multimodal infrastructure
- TencentDB Agent Memory follow-up research
- A custom domain
- Changes to any separate production deployment or its memory
- A large corpus or an embedding model comparison
- New retrieval features that are not needed for the demo story

## P0 release baseline

Target completion is August 15 at noon JST.

- [x] Format `src/aizk/ops/probes.py`, `src/aizk/store/security.py`, and
  `tests/store/test_artifact.py`
- [x] Fix the eight mypy errors in `store/vector.py`, `retrieval/models/lane.py`,
  `store/models/views/live_fact.py`, `store/models/namespaces.py`, and
  `artifacts/description.py`
- [x] Review every uncommitted and untracked file and separate submission work from unrelated work
- [x] Keep OCR as post-hackathon production work and retain the bounded artifact demo path
- [x] Run the complete Python test suite with 1,767 passing tests and 100 percent coverage
- [x] Run documentation diagnostics, the 94-page build, page checks, and brand drift checks
- [x] Synthesize and diff the deployed CDK stack before both August 13 deployments
- [x] Run package lint, import contracts, all three type checkers, and frontend checks on the final release candidate
- [x] Run every test and infrastructure check once more from the exact submitted commit
- [ ] Run the local Lambda workload once from a clean database after the gates pass
- [x] Keep the existing dated local baselines. No superseding local result is needed
- [x] Have the project owner approve and commit the coherent release candidate
- [x] Push the release candidate and confirm GitHub checks pass on `main`

Acceptance requires a clean release candidate with every required gate green. The working tree may
contain unrelated user work only if it is clearly excluded from the submitted commit.

## P0 storefront and dashboard restoration

- [x] Use one canonical Claude Code and Codex setup definition across the homepage, quickstart,
  `setup.md`, client pages, and repository README
- [x] Validate both local plugin manifests and their marketplace metadata
- [x] Add the production Svelte dashboard and plugin consistency checks to GitHub Actions
- [x] Commit and push the marketplace files so the public install commands resolve from GitHub
- [x] Deploy the production SvelteKit dashboard behind the existing AWS Function URL
- [x] Create a separate Traditional Web Logto client and store both web secrets in SSM
- [x] Verify the public routes, OAuth discovery, sign-in redirect, image digests, queue state, and
  zero Lambda errors or throttles
- [ ] Complete one signed-in dashboard pass in an isolated rendered browser

Acceptance requires both public marketplace commands to install from GitHub and one signed-in
browser pass through the dashboard. The deployed application is ready, but the current marketplace
files remain local until the project owner approves the release commit and push.

## P0 restore cloud control

Target completion is August 14 at 3 PM JST.

- [x] Keep Distributed Vector Indexing and ccloud as meaningful CockroachDB integrations
- [x] Add the Managed MCP and Agent Skills queue steward with a read-only tool allowlist
- [x] Create the cluster-scoped service account key and capture a redacted live verdict
- [x] Inspect cluster health, schema, indexes, and current migration state through ccloud
- [x] Record the current ccloud operator identifier outside the public repository
- [x] Keep the cluster UUID and Managed MCP service key out of screenshots
- [x] Add a self-contained AWS CLI 2.36.20 wrapper and expose the commands used by the deployment guide
- [x] Verify AWS caller identity and selected account without printing credentials
- [x] Confirm `ap-southeast-1` as the only AWS runtime region
- [x] Record the unused $100 promotional credit and its August 11, 2027 expiration
- [x] Keep the active AWS Budget without optional direct email notifications for the demo
- [x] Confirm the isolated Logto tenant and applications for AIZK
- [x] Confirm the OpenRouter demonstration key is available for SSM

The CockroachDB Cloud cluster runs in AWS Singapore. Public evidence omits its cluster UUID,
connection string, service account key, and operator identity. The queue steward reads the cluster
UUID from the ignored local environment.

## P0 prepare the cloud database

Target completion is August 14 at 6 PM JST.

- [x] Inspect `craizk_staging` before making any destructive decision
- [x] Capture the Alembic head and compare it with the single CockroachDB `0001` baseline
- [x] Keep the proven staging database. No destructive reset is required
- [x] Keep the migration owner and restricted application user separate
- [x] Revoke default `admin` membership from `aizk_app`
- [x] Apply the single CockroachDB migration through the setup path
- [x] Verify row security is forced and the application user has no admin role grant
- [x] Verify the C-SPANN projection and all supporting scope indexes exist
- [x] Verify queue state, corpus counts, scoped vector count, and grants
- [x] Run one restricted application write and read through the hosted MCP endpoint
- [x] Capture a redacted schema inventory for later evidence

Acceptance requires the current staging database to match the single baseline and work with the
restricted application identity. It must retain only the approved public demonstration corpus.

## P0 deploy the AWS environment

Target completion is August 13 at 8 PM JST.

- [x] Bootstrap CDK in `ap-southeast-1`
- [x] Deploy the repository-only stack and record the ECR output
- [x] Build the exact Lambda target for `linux/amd64` without provenance metadata
- [x] Push one immutable image and record its digest
- [x] Store both database URLs and the OpenRouter key in SSM SecureString
- [x] Store the Logto management secret after its application exists
- [x] Create the AIZK Logto management application and browser SPA, plus the public Native MCP application
- [x] Configure the `control` scope and the required OAuth audience
- [x] Confirm the direct AWS Function URL deployment sequence
- [x] Protect the pre-Logto endpoint with AWS IAM
- [x] Redeploy with the stable Function URL as `AIZK_AWS_PUBLIC_URL`
- [x] Update the Logto redirects and resource configuration to the same final URL
- [x] Deploy the site, docs, user UI, API and modern MCP routes on the same origin with Logto OAuth
- [x] Deploy private S3 with a 4 MiB file cap and 1 GiB cumulative quota per user
- [x] Complete authenticated upload, worker extraction and find through the public endpoint
- [x] Invoke the worker setup event and save its successful migration receipt
- [x] Confirm the worker recovery schedule is enabled
- [x] Keep the active AWS Budget without optional direct email notifications for the demo
- [x] Confirm log retention, quotas, the account concurrency cap, and the ten dollar budget
- [x] Record stack outputs without exposing parameter values
- [x] Replace the embedded OAuth proxy with direct Logto verification
- [x] Register the exact stable Codex callback and verify Logto accepts the PKCE request
- [x] Delete the obsolete confidential OAuth application and SSM secret
- [x] Deploy final MCP and worker image `sha256:3280ad739f7021ee025432c33d68d4cbfcccd0cf93cdc3ed888ba087f8fb21fa`
- [x] Deploy final web image `sha256:00d28570ff61b6e985da81ad93aebdda39abbd17e0941594172eeeb6c6df8b50`

The deployment must use the AWS assigned Function URL. It must not reuse any separate production
endpoint.

## P2 optional cloud verification automation

- [x] Defer extra cloud workload automation until after the submission
- [x] Exercise the judge-visible OAuth flow without logging its token
- [x] Preserve the modern MCP metadata on every request in the live authentication probe
- [x] Verify Codex `0.147.0` discovers the direct Logto client and generates the accepted callback
- [x] Verify pinned OpenCode `1.18.8` signs in through direct Logto and calls `status`
- [x] Confirm OpenCode `1.18.18` still lacks MCP `2026-07-28` compatibility
- [x] Add a redacted environment receipt naming region, database version, image digest, and protocol
- [x] Capture separate cold and warm benchmark phases in the August 12 cloud result
- [x] Record per-query timings and evidence marker counts
- [x] Record status, queue, document, chunk, vector, and failure counts
- [x] Write the result to a dated JSON file under `hackathon/results`

Cloud mode and direct HTTPS workload automation remain future work. The public client instructions
and dated cloud evidence already satisfy the submission story.

## P0 cloud smoke and performance gate

The public URL deadline is August 14 at 6 PM JST. The final evidence deadline is August 15 at
10 PM JST.

- [x] Open the AWS URL from a signed-out isolated browser outside the development profile
- [x] Complete direct OAuth and call `status` as the public demonstration identity
- [x] Call `status` and verify limits, identity, processing state, and zero retained failures
- [x] Call `keep` with one short public note
- [x] Confirm the MCP Lambda wakes the worker Lambda
- [x] Confirm the durable queue reaches zero pending, running, and failed tasks
- [x] Call `find` with different wording and `web` set to `off`
- [x] Verify the answer cites the exact source excerpt and document handle
- [x] Verify the privacy receipt reports no web egress
- [x] Run ten warm `find` calls across each compatible embedding provider
- [x] Capture medians and maxima without presenting ten calls as a stable benchmark
- [x] Confirm every warm MCP handler finishes below its 60 second Lambda timeout
- [x] Capture the direct C-SPANN plan from CockroachDB Cloud
- [x] Keep the direct redacted C-SPANN plan and skip the optional composed plan capture
- [x] Capture Lambda duration, errors, throttles, and memory use from CloudWatch
- [x] Confirm no request or model payload appears in the reviewed MCP and worker logs
- [x] Capture one clean ccloud cluster inspection
- [x] Capture one healthy service-key Managed MCP queue steward verdict
- [x] Save a dated cloud result JSON
- [x] Save a redacted command transcript

The preferred warm gate is p95 below eight seconds, no timeouts, and no retained failures. The SWE
cloud run passed with a 3.16 second warm p95, no Lambda errors or throttles, and an idle queue. The
honest cold post-deployment maximum was 32.68 seconds. The five-minute warm schedule normally pays
that initialization before a user request. CockroachDB execution now dominates warm latency.
[The complete measurement record](PERFORMANCE.md) names every tradeoff.

## P1 choose and load the final corpus

Target completion is August 15 at 3 PM JST.

- [x] Use the six public SWE practice notes as the final bounded demo corpus
- [x] Drop optional papers, projects, PDFs, image OCR, and the 87-document local corpus from the cloud critical path
- [x] Review and record the public references and terms already named by the six notes
- [x] Exclude private notes, production memory, Japanese scans, and retained third-party secrets
- [x] Freeze the corpus cap at six source notes for the recorded demo
- [x] Load the six-note rehearsal corpus with concurrency one
- [x] Drain rehearsal extraction fully and confirm no failed or unreadable items
- [x] Freeze the six-note baseline and recorded Project Atlas source after the final rehearsal
- [x] Update `DISCLOSURE.md` with the exact six-note corpus and its use of linked public references

The smallest credible corpus wins. External material is optional. It should be dropped immediately
if it delays the public endpoint or weakens provenance.

## P1 judge access and clean rehearsal

Target completion is August 16 at noon JST.

- [x] Create the nonprivate Maya Chen demonstration identity
- [x] Give it the demo-only Northstar organizations and current minimum application permissions
- [x] Keep separate production deployments and personal scopes out of its visible memory
- [x] Write exact direct Logto Codex connection instructions
- [x] Include the AWS assigned MCP URL, account instructions, expected source, and expected query
- [x] Refresh and verify Maya's secure SSM demo-directory password after the recording rehearsal
- [ ] Test the complete flow in a signed-out isolated browser and clean Codex profile
- [ ] Test from a network that is not the development machine
- [x] Verify the exact public commit from a clean Docker clone using only committed files
- [x] Confirm the repository, docs, demo, and video need no local path or private DNS
- [x] Confirm the fictional demo account signs in directly without owner approval
- [ ] Verify rate limits allow the complete judge flow twice

Acceptance requires a new tester to finish the story in under five minutes without asking a
question.

## P1 evidence package

Target completion is August 16 at 8 PM JST.

- [x] Capture a grounded `find` result with source excerpt, document handle, and privacy receipt
- [x] Capture document, chunk, claim, scoped vector, and queue schema and counts
- [x] Preserve the redacted cloud C-SPANN plan showing the distributed vector index and 7 ms execution
- [x] Capture the successful setup and worker Lambda invocations
- [x] Capture Lambda log review, the gross-cost budget, and both EventBridge schedules
- [x] Capture ccloud cluster inspection with no connection string
- [x] Capture the redacted queue steward verdict with its Managed MCP tools and Agent Skills
- [x] Export the final architecture diagram at a readable resolution
- [x] Select, crop, and inspect the final Devpost gallery from the privacy-reviewed 1080p footage
- [x] Check the complete walkthrough for credentials, account identifiers, unrelated stacks, and private data
- [x] Keep the current branded social thumbnail
- [x] Link every numerical claim in `SUBMISSION.md` to a dated result file
- [x] Keep local and cloud claims explicitly separated by their evidence

Acceptance requires every judging criterion to have at least one visible piece of evidence.

## P1 video

Target completion is August 17 at 8 PM JST.

- [x] Update `VIDEO_SCRIPT.md` with the final cloud numbers and exact screens
- [x] Rehearse once while timing every section
- [x] Increase terminal, console, and browser text for 1080p readability
- [x] Record editable takes for every required section
- [x] Show a real write, durable worker completion, C-SPANN evidence, grounded read, and Lambda operation
- [x] Keep third-party copyrighted material and unrelated trademarks out of frame
- [x] Remove every credential and private identifier from the complete walkthrough
- [ ] Keep the final cut under two minutes and fifty seconds
- [ ] Add accurate English captions
- [ ] Upload to YouTube or Vimeo
- [ ] Verify playback, audio, captions, and resolution while signed out
- [ ] Put the final video URL into Devpost and `SUBMISSION.md`

## P1 Devpost entry

Create the draft by August 14. Finish it by August 17 at 10 PM JST.

- [ ] The project owner confirms age, country, Devpost account standing, and entrant eligibility
- [ ] Create the project draft before cloud work is finished
- [ ] Enter the title, elevator pitch, category, repository, and current documentation URL
- [ ] Fill every narrative field from `SUBMISSION.md`
- [x] Prepare copy identifying Distributed Vector Indexing and ccloud as the required tools
- [x] Prepare copy explaining the concrete role of each CockroachDB tool
- [x] Prepare copy identifying Lambda and the supporting AWS services actually deployed
- [x] Prepare copy explaining CockroachDB as persistent memory rather than a side database
- [ ] Add the architecture diagram
- [x] Add the functional AWS demo URL and judge instructions to the submission copy
- [ ] Add the public video URL
- [x] Add the pre-existing work disclosure to the submission copy
- [x] Omit optional sponsor feedback unless the final portal asks for it
- [ ] Preview every field for truncation and broken Markdown
- [ ] Save a copy of the final entered text in `SUBMISSION.md`

## Final release gate

Target completion is August 18 at noon JST.

- [ ] Freeze code and corpus
- [x] Run every local gate from the current working tree
- [x] Run every local gate from the submitted commit
- [x] Confirm GitHub checks are green for the current release candidate
- [x] Deploy the final API and web image digests and verify the unchanged public URL
- [ ] Run the complete cloud smoke flow twice
- [x] Confirm no retained queue failures, Lambda errors, or throttles
- [ ] Open repository, docs, AWS demo, and video from a signed-out browser
- [ ] Test the judge account from a clean MCP client profile
- [ ] Verify the video is under three minutes and public
- [ ] Verify every Devpost field is complete
- [ ] Verify no claim uses local evidence as a cloud measurement
- [ ] Verify all screenshots and logs are free of secrets and private data
- [x] Confirm the ten dollar gross-cost budget remains active. Email notification is optional
- [ ] Confirm the service can remain online through September 16 at 6 AM JST
- [ ] The project owner performs the final submission by August 18 at 6 PM JST
- [ ] Reopen the submitted project page and verify all links after submission

## Daily timeline

| Date | Main outcome | Hard checkpoint |
| --- | --- | --- |
| August 11 | Freeze scope, establish this plan, and inventory real blockers | No new feature work enters the critical path |
| August 12 | Cloud corpus, performance result, C-SPANN plan, and isolated client rehearsals | Cloud evidence proves the core story |
| August 13 | Direct Logto OAuth, final Lambda image, and obsolete proxy removal | Deployed product path is complete |
| August 14 | Direct interactive login, ccloud inspection, redacted evidence, and clean external rehearsal | A judge can complete the exact flow |
| August 15 | Release candidate cleanup, full gates, commit, push, and corpus freeze | Submitted code and evidence are immutable |
| August 16 | Clean judge rehearsal, screenshots, final architecture and narrative | A stranger can finish the demo in five minutes |
| August 17 | Record, edit, upload, and verify video, then complete Devpost | Submission package complete by 10 PM |
| August 18 | Full release gate, signed-out review, and early submission | Submit by 6 PM with a 12 hour buffer |
| August 19 | Emergency buffer only | Official deadline at 6 AM |
| Through September 16 | Keep the project free, reachable, and unchanged except for repairs | Daily health and budget check |

## Go or stop checkpoints

### August 12 at 8 PM

Continue only if the release baseline is green, the cluster is inspectable, AWS identity works, and
the Logto and SSM inputs are available. Escalate missing credentials to the project owner immediately.

### August 14 at 6 PM

Continue to media production only if a judge can authenticate and complete `status`, `keep`, and
`find` through the AWS URL. If not, stop corpus expansion and all optional polish.

### August 15 at 10 PM

Freeze the architecture only if requests stay below the API limit, queue failures are zero, and
the evidence contains a real C-SPANN plan. Use local numbers with their exact conditions if cloud
measurements are too small, but never present them as cloud performance.

### August 17 at 10 PM

The video, demo URL, repository, and narrative must all exist. August 18 is only for verification
and submission.

## Owner decisions and manual actions

- [x] Create the cluster-scoped Managed MCP service key
- [x] Confirm the AWS account
- [x] Keep the active budget without optional direct email notifications
- [x] Confirm the dedicated Logto applications and judge identity
- [x] Keep the current `craizk_staging` database without a destructive reset
- [x] Approve the bounded six-note public corpus and no external expansion
- [ ] Confirm Devpost eligibility and create the draft
- [ ] Review and commit the release candidate
- [ ] Approve the final video and submission text
- [ ] Press the final Devpost submit button by the internal cutoff

## Estimated focused effort

These are planning estimates rather than measured durations.

| Workstream | Focused hours |
| --- | --- |
| Release baseline and cleanup | 4 to 6 |
| Cloud access and database verification | 1 to 2 |
| AWS and identity deployment | Complete |
| Cloud workload and performance evidence | 2 to 4 |
| Corpus, evidence, and clean rehearsal | 4 to 6 |
| Video and Devpost package | 8 to 12 |
| Final verification and submission | 3 to 5 |
| Remaining total | 22 to 35 |

The service-key steward run is complete. The final video and Devpost entry remain the submission
path. Corpus expansion, OCR, and new features remain optional.

## After submission

- [ ] Check endpoint health, queue failures, Lambda errors, throttles, and spend once each day
- [ ] Keep the repository, account, demo, and video accessible without charge through judging
- [ ] Do not rotate judge credentials unless they are compromised
- [ ] Do not reset the database or delete the AWS stack during judging
- [ ] Make only narrowly necessary repairs and document every change
- [ ] Preserve the submitted image digest and dated result files
- [ ] Retire the judge account and cloud resources only after September 16 at 6 AM JST

## Definition of done

AIZK is done when a signed-out judge can follow the submitted instructions and connect to the AWS
assigned MCP URL. The supplied nonprivate account must authenticate without owner intervention. The
judge can then keep a public memory while CockroachDB and Lambda complete the durable work. A later
`find` must retrieve grounded evidence through C-SPANN, with the public repository and short video
completing the story.
