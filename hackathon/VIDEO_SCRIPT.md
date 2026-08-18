# Video script

Target length is two minutes and forty-five seconds. The final recording must be in English, show
the working product and CockroachDB memory layer, and remain under three minutes.

Keep raw recordings and edit project files outside the source repository.

## Demo website

```text
https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/
```

## Prepare before recording

Use a 1920 by 1080 canvas at 30 frames per second. Set browser zoom to at least 125 percent and the
terminal font to at least 22 pixels. Hide bookmarks, notifications, account menus, shell history,
AWS account identifiers, connection strings, tokens, email addresses, and unrelated deployments.

Prepare these five views before starting.

1. The deployed AIZK landing page and architecture diagram
2. A clean Codex profile connected through direct Logto OAuth
3. The redacted C-SPANN and AWS gallery images in the public repository
4. The redacted queue steward verdict with Managed MCP tools and Agent Skills
5. CloudWatch Lambda duration, error, and throttle charts with account details cropped out

Use this public demonstration note.

```text
# Project Atlas release policy

Project Atlas deploys one immutable artifact to staging and production. Its release gate checks p95
latency and rollback rate before promotion.
```

Ask this differently worded question after the worker finishes.

```text
What should Atlas verify before promoting a release? Search only memory and keep web off.
```

Use a clean Codex environment and follow the two commands shown on the AIZK landing page. Complete
Logto sign-in before the recorded take. In Codex, ask it to call only `status`, then to keep the
Atlas note exactly, then to call `status` until processing is idle, and finally to ask the prepared
question with `web="off"`.

## Opening from 0 to 15 seconds

Show the deployed landing page, then move directly to the connected Codex client.

> Agents often keep detached text without its source or access boundary. AIZK gives MCP
> agents durable, scoped memory on CockroachDB Cloud and AWS Lambda. I will write one project policy
> and retrieve it using different words.

## Live write from 15 to 45 seconds

Call `status`, then `keep` with the prepared Atlas note. Pause on the authenticated demo identity,
the returned document handle, and the processing state. Do not show the Logto administration page.

> Codex signs in through a public PKCE client with no shared secret. The write enters CockroachDB
> under the caller's scope, returns a document handle, and wakes a private worker Lambda.

## Durable processing from 45 to 70 seconds

Show the worker invocation and the queue returning to zero pending, running, and failed work. Then
show the redacted cloud inventory with documents, chunks, facts, entities, communities, and scoped
vectors.

> CockroachDB is not a side database here. It holds sources, vectors, temporal facts, authorization,
> usage, and the durable queue in one transactional system. The worker can resume without losing
> the memory operation.

## Grounded find from 70 to 115 seconds

Call `find` with the prepared question and `web` set to `off`. Pause on the Atlas source excerpt,
document handle, and privacy receipt. Highlight that the wording differs from the stored note.

> The question never repeats the stored sentence. C-SPANN finds the scoped source, and AIZK returns
> the exact excerpt and document handle rather than an untraceable summary. The privacy receipt
> confirms that no web search happened.

## CockroachDB tools from 115 to 140 seconds

Show the committed redacted C-SPANN plan, then the queue steward verdict. Keep each view on screen
long enough to read the tool and skill names.

> Distributed Vector Indexing selected the scoped C-SPANN index and executed the cloud probe in seven
> milliseconds. The steward uses Managed MCP and the official CockroachDB Agent Skills to inspect
> queue and database failures without exposing write tools. Repairs still require approval. ccloud
> separately manages the cluster and restricted SQL identities.

## AWS boundary from 140 to 160 seconds

Show the architecture diagram beside the redacted CloudWatch charts. Briefly show the private S3
bucket label, recovery schedule, and ten dollar budget without account identifiers.

> One public Lambda serves MCP and the UI. A private Lambda handles durable processing, S3 preserves
> bounded uploads, and EventBridge recovers delayed work. CloudWatch showed zero errors and
> throttles. Warm find measured a 2.14 second median and 3.16 second p95.

## Close from 160 to 165 seconds

Return to the grounded answer and place the public repository URL beside it.

> AIZK gives agents memory they can question, not a cache they must trust. The source stays
> visible, the boundary stays in CockroachDB, and the memory survives the agent.

## Recording and editing checklist

- [x] Complete the direct Logto sign-in and Atlas rehearsal before recording
- [x] Use only the Maya Chen demonstration identity and one Atlas demo write
- [ ] Record the live write, worker completion, grounded read, C-SPANN plan, queue steward, and Lambda metrics
- [x] Capture each required action in editable source footage
- [x] Keep browser and terminal text readable at 1080p without zooming in during editing
- [ ] Record clean English narration separately if live narration slows the actions
- [ ] Use cuts and short crossfades only. Do not add copyrighted music
- [x] Remove every credential, email, account identifier, connection string, and unrelated deployment
- [ ] Keep the final cut between two minutes and thirty seconds and two minutes and fifty seconds
- [ ] Add accurate English captions and inspect every caption against the narration
- [ ] Upload to YouTube or Vimeo as public
- [ ] Verify video, audio, captions, 1080p resolution, and every link while signed out
- [ ] Put the final video URL into Devpost and `SUBMISSION.md`
