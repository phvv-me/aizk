// Logto OAuth resource-server authentication, ported from the Python aizk.mcp.auth stack.
// The token path mirrors fastmcp's JWTVerifier: Bearer JWTs are checked against the
// tenant's OIDC discovery document (trusted issuer, JWKS, advertised signing algorithms),
// must carry the exact /mcp resource indicator as audience plus every required scope, and
// any failure answers 401 with the RFC 9728 resource_metadata pointer so MCP clients can
// start the OAuth flow. Authority then mirrors the Python LogtoClient: the caller's Logto
// organizations (Management API, fail-closed to none) become read scopes, memberships with
// a writable role become write scopes, and organizations marked public become the public
// scopes every caller sees. Identity is the same uuid5 derivation as the Python
// settings.subject_id and scope_id, so both servers agree on every row. Reads its AIZK_*
// environment variables directly with the Python setting names; settings.ts stays as is.
import { Buffer } from 'node:buffer';
import { env } from 'node:process';

import { createRemoteJWKSet, jwtVerify, type JWTPayload, type JWTVerifyGetKey } from 'jose';
import { v5 as uuidv5 } from 'uuid';
import { z } from 'zod';

import type { User } from '../lib/server/db';

// Matches fastmcp's enhanced RequireAuthMiddleware description for invalid bearer tokens.
const INVALID_TOKEN_DESCRIPTION =
	'Authentication failed. The provided bearer token is invalid, expired, or no longer ' +
	'recognized by the server. To resolve: clear authentication tokens in your MCP client ' +
	'and reconnect. Your client should automatically re-register and obtain new tokens.';

const discoverySchema = z.object({
	issuer: z.string(),
	jwks_uri: z.string(),
	token_endpoint: z.string(),
	id_token_signing_alg_values_supported: z.array(z.string()).min(1)
});

// The identity subset of the Python Claims model; jose already verified iss, aud, and exp.
const claimsSchema = z.object({
	sub: z.string().trim().min(1),
	iat: z.number().positive(),
	exp: z.number().positive(),
	name: z.string().optional(),
	preferred_username: z.string().optional(),
	username: z.string().optional()
});

const organizationSchema = z.object({
	id: z.string().trim().min(1),
	name: z.string().trim().min(1),
	customData: z.record(z.string(), z.unknown()).default({}),
	organizationRoles: z.array(z.object({ name: z.string() })).default([])
});

const tokenSchema = z.object({
	access_token: z.string().min(1),
	expires_in: z.number().positive().default(3600)
});

type Discovery = z.infer<typeof discoverySchema>;
type Organization = z.infer<typeof organizationSchema>;

interface Config {
	logtoUrl: string;
	publicUrl: string;
	clientId: string;
	clientSecret: string;
	managementResource: string;
	requiredScopes: string[];
	writableRoles: string[];
	cacheSeconds: number;
	identityUrl: string;
	anonymousUserId: string;
}

interface Cached<V> {
	value: V;
	until: number;
}

const stripSlash = (url: string): string => url.replace(/\/+$/, '');

// AIZK_LOGTO_REQUIRED_SCOPES and friends accept the Python JSON-list form or a comma list.
const listVariable = (name: string, fallback: string[]): string[] => {
	const raw = env[name]?.trim();
	if (!raw) return fallback;
	if (raw.startsWith('[')) return z.array(z.string()).parse(JSON.parse(raw));
	return raw
		.split(',')
		.map((item) => item.trim())
		.filter(Boolean);
};

// Python's fastmcp extracts scopes from the `scope` claim first, then `scp`.
const tokenScopes = (payload: JWTPayload): string[] => {
	for (const claim of ['scope', 'scp']) {
		const value = payload[claim];
		if (typeof value === 'string') return value.split(/\s+/).filter(Boolean);
		if (Array.isArray(value)) return value.filter((item) => typeof item === 'string');
	}
	return [];
};

export class LogtoAuth {
	private discovered?: Promise<Discovery>;
	private keys?: JWTVerifyGetKey;
	private management?: Cached<string>;
	private memberships = new Map<string, Cached<Organization[]>>();
	private publics?: Cached<Organization[]>;

	constructor(private readonly config: Config) {}

	// Mirrors settings.complete_auth: no AIZK_LOGTO_URL means the dev fallback, a partial
	// Logto configuration is rejected outright.
	static fromEnv(): LogtoAuth | undefined {
		const logtoUrl = env.AIZK_LOGTO_URL;
		if (!logtoUrl) return undefined;
		const required = {
			mcp_public_url: env.AIZK_MCP_PUBLIC_URL,
			logto_client_id: env.AIZK_LOGTO_CLIENT_ID,
			logto_client_secret: env.AIZK_LOGTO_CLIENT_SECRET
		};
		const missing = Object.entries(required)
			.filter(([, value]) => !value)
			.map(([name]) => name);
		if (missing.length > 0) {
			throw new Error(`Logto authentication requires ${missing.join(', ')}`);
		}
		return new LogtoAuth({
			logtoUrl: stripSlash(logtoUrl),
			publicUrl: stripSlash(env.AIZK_MCP_PUBLIC_URL as string),
			clientId: env.AIZK_LOGTO_CLIENT_ID as string,
			clientSecret: env.AIZK_LOGTO_CLIENT_SECRET as string,
			managementResource: env.AIZK_LOGTO_MANAGEMENT_RESOURCE ?? 'https://default.logto.app/api',
			requiredScopes: listVariable('AIZK_LOGTO_REQUIRED_SCOPES', ['control']).sort(),
			writableRoles: listVariable('AIZK_LOGTO_WRITABLE_ROLES', ['admin', 'editor']),
			cacheSeconds: Number(env.AIZK_LOGTO_CACHE_SECONDS ?? 60),
			identityUrl: stripSlash(env.AIZK_IDENTITY_URL ?? 'https://aizk.phvv.me'),
			anonymousUserId:
				env.AIZK_ANONYMOUS_USER_ID ?? '00000000-0000-0000-0000-000000000000'
		});
	}

	get issuer(): string {
		return `${this.config.logtoUrl}/oidc`;
	}

	// The RFC 8707 resource indicator this server is, the `aud` a valid token must carry.
	get resource(): string {
		return `${this.config.publicUrl}/mcp`;
	}

	// RFC 9728 §3.1 inserts the well-known segment between the host and the resource path.
	get metadataPath(): string {
		return `/.well-known/oauth-protected-resource${new URL(this.resource).pathname}`;
	}

	get metadataUrl(): string {
		return `${new URL(this.resource).origin}${this.metadataPath}`;
	}

	// The protected resource metadata document the Python RemoteAuthProvider serves.
	metadata(method: string = 'GET'): Response {
		const cors = {
			'access-control-allow-origin': '*',
			'access-control-allow-methods': 'GET, OPTIONS',
			'access-control-allow-headers': 'authorization, mcp-protocol-version'
		};
		if (method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
		return Response.json(
			{
				resource: this.resource,
				authorization_servers: [this.issuer],
				scopes_supported: this.config.requiredScopes,
				bearer_methods_supported: ['header'],
				resource_name: 'aizk'
			},
			{ headers: { 'cache-control': 'public, max-age=3600', ...cors } }
		);
	}

	// Resolve the caller from the request, or the exact 401 the Python middleware sends.
	async authenticate(request: Request): Promise<User | Response> {
		const header = request.headers.get('authorization');
		if (header === null) return this.challenge();
		if (!/^bearer /i.test(header)) return this.rejection();
		const payload = await this.verify(header.slice(7));
		if (payload === null) return this.rejection();
		return this.resolve(payload);
	}

	// RFC 6750 §3.1: a request with no credentials gets a challenge without an error code.
	private challenge(): Response {
		return new Response(null, {
			status: 401,
			headers: { 'www-authenticate': `Bearer resource_metadata="${this.metadataUrl}"` }
		});
	}

	private rejection(): Response {
		const parts = [
			'error="invalid_token"',
			`error_description="${INVALID_TOKEN_DESCRIPTION}"`,
			`resource_metadata="${this.metadataUrl}"`
		];
		return Response.json(
			{ error: 'invalid_token', error_description: INVALID_TOKEN_DESCRIPTION },
			{ status: 401, headers: { 'www-authenticate': `Bearer ${parts.join(', ')}` } }
		);
	}

	// Read and validate the tenant's discovery document once, as the Python client does.
	private discovery(): Promise<Discovery> {
		this.discovered ??= (async () => {
			const url = `${this.issuer}/.well-known/openid-configuration`;
			const response = await fetch(url, { headers: { accept: 'application/json' } });
			if (!response.ok) throw new Error(`Logto discovery failed with ${response.status}`);
			const discovery = discoverySchema.parse(await response.json());
			if (stripSlash(discovery.issuer) !== this.issuer) {
				throw new Error('Logto discovery returned a different issuer');
			}
			return discovery;
		})();
		return this.discovered;
	}

	// Signature via the tenant JWKS, then the same issuer, audience, expiry, and required
	// scope checks fastmcp's JWTVerifier applies; any failure verifies to nothing.
	private async verify(token: string): Promise<JWTPayload | null> {
		const discovery = await this.discovery();
		this.keys ??= createRemoteJWKSet(new URL(discovery.jwks_uri));
		try {
			const { payload } = await jwtVerify(token, this.keys, {
				issuer: stripSlash(discovery.issuer),
				audience: this.resource,
				algorithms: discovery.id_token_signing_alg_values_supported
			});
			const granted = new Set(tokenScopes(payload));
			if (!this.config.requiredScopes.every((scope) => granted.has(scope))) return null;
			return payload;
		} catch {
			return null;
		}
	}

	// The Python Auth.resolve: verified claims become the caller, invalid identity claims
	// fall back to the anonymous reader, and every caller sees the public organizations.
	private async resolve(payload: JWTPayload): Promise<User> {
		const publicScopes = (await this.publicOrgs()).map((org) => this.scopeId(org.id));
		const claims = claimsSchema.safeParse(payload);
		if (!claims.success) {
			console.warn('verified Logto token carried invalid identity claims:', claims.error);
			return { id: this.config.anonymousUserId, read: [], write: [], public: publicScopes };
		}
		const id = this.subjectId(claims.data.sub);
		const organizations = await this.userOrgs(claims.data.sub);
		const writable = (org: Organization): boolean =>
			org.organizationRoles.some((role) => this.config.writableRoles.includes(role.name));
		return {
			id,
			label:
				claims.data.name ?? claims.data.preferred_username ?? claims.data.username,
			read: [id, ...organizations.map((org) => this.scopeId(org.id))],
			write: [id, ...organizations.filter(writable).map((org) => this.scopeId(org.id))],
			public: publicScopes
		};
	}

	// Python settings.subject_id: uuid5(NAMESPACE_URL, "<identity>/subjects/<subject>").
	private subjectId(subject: string): string {
		return uuidv5(`${this.config.identityUrl}/subjects/${subject}`, uuidv5.URL);
	}

	// Python settings.scope_id: uuid5(NAMESPACE_URL, "<identity>/scopes/<external>").
	private scopeId(external: string): string {
		return uuidv5(`${this.config.identityUrl}/scopes/${external}`, uuidv5.URL);
	}

	// One user's organizations and roles from the Management API, cached briefly per
	// subject and failing closed to no authority, exactly like LogtoClient.user_orgs.
	private async userOrgs(subject: string): Promise<Organization[]> {
		const cached = this.memberships.get(subject);
		if (cached && Date.now() < cached.until) return cached.value;
		try {
			const path = `api/users/${encodeURIComponent(subject)}/organizations`;
			const value = z
				.array(organizationSchema)
				.parse(await this.managementGet(`${this.config.logtoUrl}/${path}`));
			this.memberships.set(subject, this.cache(value));
			return value;
		} catch (error) {
			console.warn('Logto user authority refresh failed and closed access:', error);
			return [];
		}
	}

	// Organizations explicitly marked public, paginated like LogtoClient.public_orgs.
	private async publicOrgs(): Promise<Organization[]> {
		if (this.publics && Date.now() < this.publics.until) return this.publics.value;
		try {
			const organizations: Organization[] = [];
			for (let page = 1; ; page += 1) {
				const url = `${this.config.logtoUrl}/api/organizations?page=${page}&page_size=100`;
				const batch = z.array(organizationSchema).parse(await this.managementGet(url));
				organizations.push(...batch);
				if (batch.length < 100) break;
			}
			const value = organizations.filter((org) => org.customData.public === true);
			this.publics = this.cache(value);
			return value;
		} catch (error) {
			console.warn('Logto public organization refresh failed and closed access:', error);
			return [];
		}
	}

	private async managementGet(url: string): Promise<unknown> {
		const response = await fetch(url, {
			headers: {
				accept: 'application/json',
				authorization: `Bearer ${await this.managementToken()}`
			}
		});
		if (!response.ok) throw new Error(`Logto management request failed with ${response.status}`);
		return response.json();
	}

	// A cached client-credentials token for the Management API, refreshed with the same
	// thirty-second expiry margin as the Python client.
	private async managementToken(): Promise<string> {
		if (this.management && Date.now() < this.management.until) return this.management.value;
		const discovery = await this.discovery();
		const basic = Buffer.from(`${this.config.clientId}:${this.config.clientSecret}`);
		const response = await fetch(discovery.token_endpoint, {
			method: 'POST',
			headers: {
				accept: 'application/json',
				authorization: `Basic ${basic.toString('base64')}`,
				'content-type': 'application/x-www-form-urlencoded'
			},
			body: new URLSearchParams({
				grant_type: 'client_credentials',
				resource: this.config.managementResource,
				scope: 'all'
			})
		});
		if (!response.ok) throw new Error(`Logto token request failed with ${response.status}`);
		const token = tokenSchema.parse(await response.json());
		this.management = {
			value: token.access_token,
			until: Date.now() + Math.max(1, token.expires_in - 30) * 1000
		};
		return token.access_token;
	}

	private cache<V>(value: V): Cached<V> {
		return { value, until: Date.now() + this.config.cacheSeconds * 1000 };
	}
}
