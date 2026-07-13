# API

The public network surface has four MCP tools. Maintenance and evaluation stay on the SSH-only
CLI so a client token cannot rebuild, export, promote, or erase data.

## MCP tools

| Tool | Purpose |
|---|---|
| `recall(query, budget)` | Return one token-budgeted context pack from all retrieval lanes |
| `remember(text, kind)` | Capture working memory for later extraction |
| `reference(uri)` | Record a paper, URL, or file as a source |
| `share(documents, scopes)` | Copy visible documents into one authorized destination with provenance |

Every tool derives scope authority from the verified Logto user. Recall reads the caller's complete
visible union, including personal, organization, and eligible intersection rows. Writes default to
the personal singleton. Passing organization names chooses one explicit destination, including an
intersection such as A and B, and succeeds only when the caller may write every member.
`created_by` records provenance and never grants access. Sharing leaves the source unchanged and
creates a provenance-linked copy in the destination.

## Operator CLI

Run commands through `chefe run aizk` from the monorepo root.

| Command group | Commands |
|---|---|
| `aizk graph` | `rebuild`, `decay`, `reembed`, `raptor`, `forget` |
| `aizk ontology` | `define-entity`, `define-relation`, `list` |
| `aizk data` | `ingest`, `ingest-image`, `promote`, `export`, `audit` |
| `aizk eval` | `bench`, `sweep`, `scale`, `groupmem` |
| `aizk db` | `setup`, `health`, `migrate`, `makemigrations`, `install-queue`, `backup`, `restore`, `check-rls`, `tasks-status` |

The top-level process commands are `serve-mcp`, `worker`, `recall-context`, `capture-session`, and
`profile-report`.

There are no local user, organization, membership, role, or curation commands. Logto is the source
of truth for those concerns.
