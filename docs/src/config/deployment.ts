/** Return the public Native OAuth client used by every bundled agent connection. */
export function publicOAuthClientId(): string {
  const clientId = import.meta.env.AIZK_DOCS_MCP_CLIENT_ID ?? '2yqsd1rbjsxxzkyrem1xv';
  if (!/^[a-z0-9]{21}$/.test(clientId)) {
    throw new Error('AIZK_DOCS_MCP_CLIENT_ID must be one Logto application ID');
  }
  return clientId;
}
