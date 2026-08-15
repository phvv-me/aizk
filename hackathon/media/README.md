# Devpost media

Run `chefe run aizk-submission-media` from the monorepo root to rebuild the gallery from the
privacy-reviewed walkthrough and canonical brand assets. The task produces 3 to 2 gallery images at
1800 by 1200 pixels. Each gallery image remains below the Devpost 5 MB limit. The separate
architecture upload keeps a wide layout so its labels remain readable and stays below its 35 MB
limit.

## Project thumbnail

Upload `thumbnail-devpost.png` as the project thumbnail. The 1800 by 1200 image has the required
3 to 2 ratio. It is generated from the same mark and visual system as the website, documentation,
and repository banner.

## Gallery order and captions

| File | Devpost caption |
| --- | --- |
| `00-hero.jpg` | crAIZK gives agents durable memory with sources, time, and access control. |
| `01-one-action-onboarding.jpg` | One instruction sends an agent to the public setup guide. |
| `02-agent-setup-guide.jpg` | The same AWS URL serves machine-readable setup and complete documentation. |
| `03-agent-configures-aizk.jpg` | Codex reads the guide and prepares the MCP connection in an isolated workspace. |
| `05-authenticated-status.jpg` | The status tool confirms the Maya demonstration identity and durable processing health. |
| `06-live-memory-write.jpg` | The agent stores the Project Atlas policy and receives a document handle. |
| `07-grounded-recall.jpg` | A differently worded question returns the source excerpt, handle, and privacy receipt. |
| `08-memory-console.jpg` | The authenticated console makes sources and derived memory inspectable. |
| `10-cspann-plan.jpg` | The redacted CockroachDB plan selects the scoped C-SPANN vector index. |
| `11-lambda-operations.jpg` | Redacted AWS evidence shows bounded Lambda operation without errors or throttles. |
| `00-architecture.png` | CockroachDB keeps memory state while AWS runs the public MCP and private worker. |

`04-logto-sign-in.jpg` and `09-aws-architecture.jpg` are retained as supporting evidence but omitted
from the recommended gallery. The empty development-tenant form is less informative than the
authenticated status frame. The recorded architecture page still includes a reference to the
separate PostgreSQL deployment, while the generated architecture image is cleaner and specific to
the submission.

## Safety review

The source walkthrough was reviewed for credentials, email addresses, account numbers, database
connection strings, private notes, and unrelated deployments. Inspect every generated image again
before upload because a frame can expose information that is easy to miss during video playback.
