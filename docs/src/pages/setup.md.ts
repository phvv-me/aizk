import type { APIRoute } from 'astro';
import { setupFor } from '../config/setup';

export const prerender = true;

const site = (import.meta.env.SITE ?? 'https://aizk.example.com').replace(/\/$/, '');
const endpoint = `${site}/mcp`;
const clientId = import.meta.env.AIZK_DOCS_MCP_CLIENT_ID ?? 'PUBLIC_AIZK_CLIENT_ID';
const claude = setupFor('claude-code');
const codex = setupFor('codex');

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

## 1. Choose one client

Show the user the Claude Code and Codex choices. Use only the section they choose. Do not remove an
existing AIZK connection. The plugin contains both the AIZK skill and its MCP server definition.

### Claude Code

Run the following commands.

\`\`\`sh
${claude.install}
${claude.connect}
\`\`\`

If this instruction is already running inside Claude Code, do not open a nested Claude process.
Install the plugin, ask the user to restart Claude Code, then continue after restart. Approve the
plugin-provided MCP server, open \`/mcp\` and choose sign in. For a remote session, let the user
complete the browser step manually.

### Codex

\`\`\`sh
${codex.install}
${codex.connect}
\`\`\`

Let the user complete the browser step. A remote session may need an SSH forward for port 8912.

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

## 2. Guide browser authentication

Tell the user when the AIZK sign-in page opens. If they already have an account, ask them to sign in. If
they do not, ask them to choose account creation, enter their email and complete email verification.
Wait for the browser flow to finish before continuing. Never ask the user to send you their password
or authentication code.

## 3. Verify

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
