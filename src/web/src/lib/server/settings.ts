import { env } from '$env/dynamic/private';
import { homeFor, trustedOrigin } from '$lib/routes';

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
  /** The operator hostname, when the console answers on one of its own. Empty when it does not. */
  get operatorUrl(): string {
    return (env.AIZK_WEB_OPERATOR_URL ?? '').replace(/\/$/, '');
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
  /** The origin a browser on `origin` is sent back to, this deployment's own or nothing. */
  originFor(origin: string): string {
    return trustedOrigin(origin, [this.publicUrl, this.operatorUrl]);
  },
  /** The sign-in callback belonging to `origin`, the redirect Logto is asked to honour. */
  callbackFor(origin: string): string {
    return `${this.originFor(origin)}/auth/sign-in-callback`;
  },
  /** Where a completed sign-in lands, which is the console on the operator hostname. */
  homeFor(origin: string): string {
    return homeFor(this.originFor(origin), this.operatorUrl);
  },
  /** The hosted Logto Account Center for profile and credential self-service. */
  get accountUrl(): string {
    return `${this.logtoEndpoint.replace(/\/$/, '')}/account`;
  }
};
