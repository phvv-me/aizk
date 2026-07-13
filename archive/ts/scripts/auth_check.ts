// Self-contained check of the Logto resource-server auth port. A throwaway local HTTP
// server plays the whole tenant (OIDC discovery, JWKS, client-credentials token, and the
// Management API organization endpoints), tokens are self-signed with a fresh ES384 key
// in the exact claim shape the Python tests use for Logto, and the expected identity
// UUIDs are precomputed with the Python settings.subject_id/scope_id so the uuid5
// derivation is pinned across languages, byte for byte.
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { env, exit } from 'node:process';

import { exportJWK, generateKeyPair, SignJWT } from 'jose';

import type { User } from '../src/lib/server/db';

// uuid.uuid5(NAMESPACE_URL, "https://aizk.phvv.me/subjects/user-1") and friends, from Python.
const SUBJECT = 'd250ee8e-6726-533b-9daa-1225f0700fef';
const ORG_A = '7268ca0d-4507-51f6-a4d4-d60f37a18eca';
const ORG_B = '33fe0d2a-1f97-5a4b-92fb-1c7efe8389d9';
const PUBLIC_ORG = '85589713-aaa5-5624-b998-b702ed52c521';
const RESOURCE = 'https://aizk.test/mcp';

const { publicKey, privateKey } = await generateKeyPair('ES384');
const jwk = { ...(await exportJWK(publicKey)), kid: 'k1', alg: 'ES384', use: 'sig' };

const tenant = createServer((request, response) => {
	const path = (request.url ?? '').split('?')[0];
	const routes: Record<string, unknown> = {
		'/oidc/.well-known/openid-configuration': {
			issuer: `${origin()}/oidc`,
			jwks_uri: `${origin()}/oidc/jwks`,
			token_endpoint: `${origin()}/oidc/token`,
			id_token_signing_alg_values_supported: ['ES384']
		},
		'/oidc/jwks': { keys: [jwk] },
		'/oidc/token': { access_token: 'm2m', expires_in: 3600 },
		'/api/users/user-1/organizations': [
			{ id: 'org-a', name: 'Alpha', organizationRoles: [{ id: 'r1', name: 'editor' }] },
			{ id: 'org-b', name: 'Beta', organizationRoles: [{ id: 'r2', name: 'viewer' }] }
		],
		'/api/organizations': [
			{ id: 'pub-org', name: 'Public', customData: { public: true } },
			{ id: 'priv-org', name: 'Private', customData: {} }
		]
	};
	const body = routes[path];
	response.writeHead(body === undefined ? 404 : 200, { 'content-type': 'application/json' });
	response.end(JSON.stringify(body ?? { error: 'not found' }));
});
await new Promise<void>((resolve) => tenant.listen(0, '127.0.0.1', resolve));
const origin = (): string => `http://127.0.0.1:${(tenant.address() as AddressInfo).port}`;

env.AIZK_LOGTO_URL = origin();
env.AIZK_MCP_PUBLIC_URL = 'https://aizk.test';
env.AIZK_LOGTO_CLIENT_ID = 'client';
env.AIZK_LOGTO_CLIENT_SECRET = 'secret';
const { LogtoAuth } = await import('../src/mcp/auth');
const auth = LogtoAuth.fromEnv();
if (auth === undefined) throw new Error('LogtoAuth.fromEnv returned undefined');

const issue = async (claims: Record<string, unknown> = {}): Promise<string> => {
	const now = Math.floor(Date.now() / 1000);
	return new SignJWT({
		iss: `${origin()}/oidc`,
		sub: 'user-1',
		aud: RESOURCE,
		iat: now,
		exp: now + 300,
		scope: 'control',
		name: 'User One',
		client_id: 'spa',
		...claims
	})
		.setProtectedHeader({ alg: 'ES384', kid: 'k1' })
		.sign(privateKey);
};

const call = (token?: string): Promise<User | Response> =>
	auth.authenticate(
		new Request(RESOURCE, {
			headers: token === undefined ? {} : { authorization: `Bearer ${token}` }
		})
	);

const rejected = (outcome: User | Response): boolean =>
	outcome instanceof Response &&
	outcome.status === 401 &&
	(outcome.headers.get('www-authenticate') ?? '').includes('error="invalid_token"');

const same = (actual: string[], expected: string[]): boolean =>
	JSON.stringify([...actual].sort()) === JSON.stringify([...expected].sort());

const user = await call(await issue());
const challenge = await call();
const metadata = auth.metadata();
const cases: Record<string, boolean> = {
	valid_token_identity:
		!(user instanceof Response) && user.id === SUBJECT && user.label === 'User One',
	valid_token_read_scopes:
		!(user instanceof Response) && same(user.read, [SUBJECT, ORG_A, ORG_B]),
	valid_token_write_scopes: !(user instanceof Response) && same(user.write, [SUBJECT, ORG_A]),
	valid_token_public_scopes: !(user instanceof Response) && same(user.public, [PUBLIC_ORG]),
	expired_token_rejected: rejected(
		await call(await issue({ iat: 1_000_000, exp: 1_000_060 }))
	),
	wrong_audience_rejected: rejected(await call(await issue({ aud: 'https://other.test/mcp' }))),
	wrong_issuer_rejected: rejected(await call(await issue({ iss: 'https://evil.test/oidc' }))),
	missing_scope_rejected: rejected(await call(await issue({ scope: 'openid' }))),
	missing_header_challenged:
		challenge instanceof Response &&
		challenge.status === 401 &&
		(challenge.headers.get('www-authenticate') ?? '').includes(
			'resource_metadata="https://aizk.test/.well-known/oauth-protected-resource/mcp"'
		) &&
		!(challenge.headers.get('www-authenticate') ?? '').includes('error='),
	metadata_document:
		JSON.stringify(await metadata.json()) ===
		JSON.stringify({
			resource: RESOURCE,
			authorization_servers: [`${origin()}/oidc`],
			scopes_supported: ['control'],
			bearer_methods_supported: ['header'],
			resource_name: 'aizk'
		})
};

tenant.close();
const ok = Object.values(cases).every(Boolean);
console.log(JSON.stringify({ ok, ...cases }));
exit(ok ? 0 : 1);
