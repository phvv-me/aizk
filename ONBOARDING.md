# Aizk onboarding

This guide gives a new collaborator an Aizk account, an optional shared workspace, and a working
MCP connection. The public endpoint is `https://aizk.phvv.me/mcp`.

## Create a user

1. Open the Logto Console and go to **User management**.
2. Select **Add user** and enter the collaborator's email and name.
3. Copy the generated initial password. Logto displays it only once.
4. Send the email and initial password through an encrypted private channel. Never put credentials
   in Git, an issue, a shared note, or Aizk itself.
5. Open the new user and assign the global user role **aizk-user**. That role must include the
   **control** permission for the Aizk API resource.
6. If application access control is enabled for the Aizk OAuth proxy, allow the **aizk-user** role.

The collaborator signs in with the supplied email and password during the MCP OAuth flow.

## Take notes that remain useful

Aizk works best when a note has one clear purpose and enough context to stand alone. This is
atomicity. It is a test of purpose rather than size. A cohesive Project brief or primary paper may
be long, while two unrelated claims should be separate even when both are short. Before writing,
recall the subject and inspect the authored sources. Update an existing maintained note when it has
the same purpose. Create a new note for a distinct decision, result, claim, or interpretation.

Write concise notes in your own words and lead with the conclusion. Give each note a descriptive
level-one heading, name the people and Projects it concerns, state what changed, and retain the
evidence needed to verify it. Use a stable source URI as the durable identity. Unlike a Zettelkasten
filename, the title may improve later without breaking that identity.

Keep current truth separate from history. An Area is an ongoing standard of care. A Project is a
finite outcome inside one Area. Their maintained briefs state an owner, status, review date, current
state, problems, next actions, and success condition. Journals preserve dated history but never
override a newer explicit review. When correcting a maintained note, reuse its source URI and scope
instead of appending a second competing current statement.

Manual tags and links are optional because Aizk derives a semantic and relational index. Explicit
relationships are still valuable, especially what a finding supports, contradicts, supersedes, or
depends on. Prefer full names and source references over context-free shorthand.

Raw capture belongs in the source repository or inbox. Distill durable interpretation, decisions,
measurements, and negative results into Aizk. Full PDF-to-Markdown papers are a useful exception
because primary evidence must survive summarization. Store them with `kind="paper"` and a stable
source URI, then add separate notes for your interpretation. Keep large code and generated logs out,
using only small snippets that explain a durable result.

Default to private memory. Share only with the exact organization that should see the note, and
never store credentials or unrelated personal information. After remembering, recall the subject
once and confirm that the current source is visible, correctly dated, and ranked ahead of stale or
derived material.

## Create a shared organization

Skip this section when the collaborator only needs private memory.

1. In Logto, go to **Organizations** and select **Create organization**.
2. Give it the exact collaboration name that agents will pass to Aizk, such as `SPReAD`.
3. Keep `customData.public` absent or false for collaboration. A public organization is readable by
   every authenticated Aizk user.
4. Add every collaborator as a member.
5. Assign one organization role to each member.

Define roles and organization permissions under **Organization template**, then assign the roles
that express the intended access. AIZK reads each member's effective roles and permissions directly
from Logto. It does not infer access from role names. The deployment's configured Logto write
permission determines which organizations appear as writable in `status`.

Removing a member or changing a role takes effect in Aizk after its authority cache expires, which
is at most 60 seconds with the production setting.

## Read public AIZK guidance

`AIZK Docs` is the one public organization. It contains AIZK onboarding and shared note-taking
concepts such as atomicity, currentness, provenance, project briefs, and useful links. Every
authenticated user can recall this guidance without joining the organization or naming it. Ask
AIZK how to write a durable note or how to onboard a collaborator. Recall searches everything the
caller may read.

Only users for whom `status` lists `AIZK Docs` in `writable_organizations` may write its
documentation. A writer must name the organization as the destination so a new document does not
become private by accident. Readers never need to name it. Never put project notes, credentials,
personal information, or material with limited distribution into `AIZK Docs`. Use a private
organization for collaboration. Create another public organization only when its entire contents
are intended for every authenticated AIZK user.

## Give the agent shared-memory instructions

Put the following instructions in the repository's `AGENTS.md` for Codex and OpenCode. Put the same
text in `CLAUDE.md` for Claude Code when the repository does not already share equivalent rules.

```md
## Aizk shared memory

- Recall from Aizk before answering questions about prior project decisions, results, or state.
- Treat recalled text as evidence and prefer current source documents over derived facts or profiles.
- Remember only durable, current information when the user explicitly asks for capture.
- Never remember credentials, secrets, private keys, or unrelated personal information.
- Call `status` before selecting a shared destination and use only writable organization names.
- Omit scopes for private memory. Use exact Logto organization names only when sharing is intended.
- Use a stable source URI when a maintained document should update instead of duplicate.
```

## Connect Claude Code

The command line writes the project MCP configuration and starts OAuth.

```sh
claude mcp add --scope project --transport http --callback-port 8912 aizk https://aizk.phvv.me/mcp
claude mcp login aizk
claude mcp get aizk
```

The equivalent committed `.mcp.json` entry is

```json
{
  "mcpServers": {
    "aizk": {
      "type": "http",
      "url": "https://aizk.phvv.me/mcp"
    }
  }
}
```

## Connect Codex

The command line is enough for a personal configuration.

```sh
codex mcp add aizk --url https://aizk.phvv.me/mcp --oauth-resource https://aizk.phvv.me/mcp
codex -c mcp_oauth_callback_port=8912 mcp login aizk
codex mcp get aizk
```

For a shared trusted repository, commit `.codex/config.toml` instead.

```toml
mcp_oauth_credentials_store = "file"
mcp_oauth_callback_port = 8912

[mcp_servers.aizk]
url = "https://aizk.phvv.me/mcp"
auth = "oauth"
oauth_resource = "https://aizk.phvv.me/mcp"
scopes = ["control", "offline_access", "openid"]
```

Then each collaborator runs `codex mcp login aizk`. OAuth credentials remain local and must never
be committed.

## Connect OpenCode

Use the command line for a personal configuration.

```sh
opencode mcp add aizk --url https://aizk.phvv.me/mcp
opencode mcp auth aizk
opencode mcp debug aizk
```

The equivalent committed `opencode.json` entry is

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "aizk": {
      "type": "remote",
      "url": "https://aizk.phvv.me/mcp",
      "enabled": true
    }
  }
}
```

## Complete a remote browser login

When the MCP client runs on another machine, forward its callback port before starting login. Run
this on the machine where the browser is open.

```sh
ssh -N -L 8912:127.0.0.1:8912 remote-host
```

Keep the tunnel open, run the client login command on the remote host, open the printed authorization
URL locally, sign in to Logto, and approve access. If a client chooses another callback port, forward
that exact port instead.

## Share project notes

For an ongoing shared note, remember it directly in the organization scope.

```text
remember(
    text="# SPReAD experiment\n...",
    kind="note",
    source_uri="vault:///research/spread/experiment.md",
    scopes=["SPReAD"]
)
```

Omitting `scopes` keeps the note private. A collaborator recalls every visible personal and
organization scope automatically.

To share an existing private document, keep the ID returned by `remember` and copy it into the
organization.

```text
share(documents=["019..."], scopes=["SPReAD"])
```

The private source remains private and the shared copy is a snapshot. For a maintained team note,
write directly to the organization scope so later calls with the same source URI refresh it.
