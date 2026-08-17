export type SetupClientId = 'claude-code' | 'codex';

export type SetupClient = {
  id: SetupClientId;
  label: string;
  install: string;
  connect: string;
  connectLabel: string;
  note: string;
  href: string;
};

/** Return the two plugin clients in the order used by every public setup surface. */
export function supportedClients(): SetupClient[] {
  return [
    {
      id: 'claude-code',
      label: 'Claude Code',
      install:
        'claude plugin marketplace add phvv-me/aizk && claude plugin install aizk@aizk',
      connect: 'claude',
      connectLabel: 'Open Claude Code',
      note: 'Approve AIZK, open /mcp and choose sign in.',
      href: '/docs/user/clients/claude-code/',
    },
    {
      id: 'codex',
      label: 'Codex',
      install: 'codex plugin marketplace add phvv-me/aizk && codex plugin add aizk@aizk',
      connect: 'codex -c mcp_oauth_callback_port=8912 mcp login aizk',
      connectLabel: 'Sign in',
      note: 'Complete browser sign in, then open Codex normally.',
      href: '/docs/user/clients/codex/',
    },
  ];
}

/** Return one supported plugin client, failing the build if its ID drifts. */
export function setupFor(id: SetupClientId): SetupClient {
  const setup = supportedClients().find((client) => client.id === id);
  if (!setup) throw new Error(`unsupported setup client ${id}`);
  return setup;
}
