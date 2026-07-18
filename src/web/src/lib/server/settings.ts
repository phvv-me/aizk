import { env } from '$env/dynamic/private';

/** Read a required AIZK_* environment variable, failing fast when absent. */
function required(name: string): string {
  const value = env[name];
  if (!value) throw new Error(`missing required environment variable ${name}`);
  return value;
}

/** Runtime configuration mirroring the AIZK_* names the Python settings already use. */
export const settings = {
  get logtoEndpoint(): string {
    return required('AIZK_LOGTO_URL');
  },
  get clientId(): string {
    return required('AIZK_WEB_CLIENT_ID');
  },
  get clientSecret(): string {
    return required('AIZK_WEB_CLIENT_SECRET');
  },
  get publicUrl(): string {
    return required('AIZK_WEB_PUBLIC_URL').replace(/\/$/, '');
  },
  get sessionSecret(): string {
    return required('AIZK_WEB_SESSION_SECRET');
  },
  /** Origin of the browser API service the server-side loads fetch from. */
  get apiUrl(): string {
    return required('AIZK_WEB_API_URL').replace(/\/$/, '');
  },
  /** The RFC 8707 resource indicator shared with the MCP server, its token audience. */
  get apiResource(): string {
    return `${required('AIZK_MCP_PUBLIC_URL').replace(/\/$/, '')}/mcp`;
  },
  /** Distinct from the MCP server's own /auth/callback so Caddy can route both flows. */
  get callbackUrl(): string {
    return `${this.publicUrl}/auth/sign-in-callback`;
  },
  /** The hosted Logto Account Center for profile and credential self-service. */
  get accountUrl(): string {
    return `${this.logtoEndpoint.replace(/\/$/, '')}/account`;
  }
};
