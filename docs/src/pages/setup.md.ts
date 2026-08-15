import type { APIRoute } from 'astro';

export const prerender = true;

const site = (import.meta.env.SITE ?? 'https://aizk.example.com').replace(/\/$/, '');
const endpoint = `${site}/mcp`;
const clientId = import.meta.env.AIZK_DOCS_MCP_CLIENT_ID ?? 'PUBLIC_AIZK_CLIENT_ID';

const instructions = `# Set up AIZK

Complete this setup for the current project. Preserve existing configuration and instructions.
Never replace an existing file wholesale.

## Deployment

- Website ${site}
- MCP endpoint ${endpoint}
- Public OAuth client ID ${clientId}
- Operating skill ${site}/aizk/SKILL.md
- Repository instructions ${site}/aizk/AGENTS.md

The public client ID is not a secret. Never ask for an API key or client secret. The user does not
need to create an account before setup. Guide them through it during the browser sign-in step if
they do not already have one.

## 1. Install the operating guidance

Fetch the operating skill and repository instructions above.

- Codex and OpenCode use \`.agents/skills/aizk/SKILL.md\` and merge the instructions into \`AGENTS.md\`.
- Claude Code uses \`.claude/skills/aizk/SKILL.md\` and merges the instructions into \`CLAUDE.md\`.

Create parent directories when needed. Merge with existing files and keep their current rules.

## 2. Configure this MCP server

Detect which client is running and use only its section.

### Claude Code

Run the following commands.

\`\`\`sh
claude mcp add --scope user --transport http --client-id ${clientId} \\
  --callback-port 8912 aizk ${endpoint}
claude mcp login aizk
\`\`\`

For a remote session, use \`claude mcp login --no-browser aizk\` and let the user complete the
browser step.

### Codex

Merge this configuration into \`.codex/config.toml\`.

\`\`\`toml
mcp_oauth_credentials_store = "file"
mcp_oauth_callback_port = 8912

[features]
mcp_2026_07_28 = true

[mcp_servers.aizk]
url = "${endpoint}"
auth = "oauth"
scopes = ["control", "offline_access", "openid"]

[mcp_servers.aizk.oauth]
client_id = "${clientId}"
\`\`\`

Then run \`codex mcp login aizk\`. Let the user complete the browser step. A remote session may
need an SSH forward for port 8912.

### OpenCode

Merge this server into \`opencode.json\`.

\`\`\`json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "aizk": {
      "type": "remote",
      "url": "${endpoint}",
      "oauth": {
        "clientId": "${clientId}",
        "scope": "control offline_access openid",
        "callbackPort": 8912,
        "redirectUri": "http://127.0.0.1:8912/callback"
      },
      "enabled": true
    }
  }
}
\`\`\`

Then run \`opencode mcp auth aizk\` and let the user complete the browser step.

## 3. Guide browser authentication

Tell the user when the Logto page opens. If they already have an account, ask them to sign in. If
they do not, ask them to choose account creation, enter their email and complete email verification.
Wait for the browser flow to finish before continuing. Never ask the user to send you their password
or authentication code.

## 4. Verify

Restart the client if it does not reload MCP configuration automatically. Call AIZK \`status\` and
confirm it returns the signed-in user and five tools. Report the files changed and whether login and
the status check succeeded.
`;

export const GET: APIRoute = () =>
  new Response(instructions, {
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'public, max-age=300',
    },
  });
