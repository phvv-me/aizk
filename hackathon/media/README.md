# Devpost media

Run `bash hackathon/media/build.sh` from the repository root to rebuild the generated media and
validate every image. The gallery uses 3 to 2 images at 1800 by 1200 pixels. Each image remains
below the Devpost 5 MB limit. The architecture upload also stays below its 35 MB limit.

## Project thumbnail

Upload `thumbnail-devpost.png` as the project thumbnail. It uses the current three-dimensional
memory cube and the same typography and colors as the live landing page.

## Gallery order and captions

| File | Devpost caption |
| --- | --- |
| `01-live-landing.jpg` | AIZK gives people, teams, and agents memory with its shape intact. |
| `02-product-overview.jpg` | The browser dashboard makes sources, findings, processing, and connected knowledge inspectable. |
| `03-scoped-sharing.jpg` | Scope intersections and temporal correction preserve access boundaries and history. |
| `04-authenticated-dashboard.jpg` | The live dashboard shows the fictional demonstration identity and its authorized organizations. |
| `05-agent-keep-and-find.jpg` | An authenticated agent keeps a project decision and finds it again with source evidence. |
| `10-cockroachdb-cspann.jpg` | The redacted CockroachDB plan selects the scoped C-SPANN vector index. |
| `11-aws-operations.jpg` | Redacted AWS evidence shows bounded serverless operation without errors or throttles. |
| `00-architecture.png` | CockroachDB keeps memory state while AWS runs the public MCP and private worker. |

The first three images, the CockroachDB card, the AWS card, and the architecture diagram are ready.
The two authenticated images are owner captures because they require the owner's active Logto and
agent sessions. Capture them using the instructions in `../screenshots/README.md`.

Do not upload the older footage-derived frames. They retain historical interface text, internal
resource names, and an obsolete test result. They remain in this directory only because the full
demo recording still references them.

## Safety review

The recommended media was reviewed for credentials, email addresses, account numbers, database
connection strings, private notes, dates, internal resource names, old product names, and unrelated
deployments. Review the two owner captures before upload because authenticated pages can expose
information that is easy to miss.
