// The aizk MCP server in TypeScript: the same four client verbs the Python FastMCP server
// exposes, over the official SDK's streamable HTTP transport. When AIZK_LOGTO_URL is set
// the server is a Logto OAuth resource server exactly like the Python one: every /mcp
// request must carry a verified Bearer token, failures answer 401 with the RFC 9728
// resource_metadata pointer, and the protected-resource metadata document is served at
// its well-known path. Without Logto, identity falls back to the development stub
// (AIZK_DEFAULT_USER with authority over its own scope). Every database read and write
// still runs under the caller's app.scopes GUC, so Postgres enforces authority exactly
// as it does for the Python server.
import { env } from 'node:process';

import { serve } from '@hono/node-server';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js';
import { z } from 'zod';

import type { User } from '../lib/server/db';
import { recall } from '../lib/server/recall/recall';
import { reference, remember } from '../lib/server/ingest';
import { share } from '../lib/server/promote';
import { LogtoAuth } from './auth';

const DEFAULT_USER = env.AIZK_DEFAULT_USER ?? '00000000-0000-0000-0000-000000000001';

const stubUser = (): User => ({
	id: DEFAULT_USER,
	read: [DEFAULT_USER],
	write: [DEFAULT_USER],
	public: []
});

const json = (value: unknown) => ({
	content: [{ type: 'text' as const, text: JSON.stringify(value) }]
});

const buildServer = (user: User): McpServer => {
	const server = new McpServer({ name: 'aizk', version: '0.0.1' });

	server.registerTool(
		'recall',
		{
			description:
				'Recall everything the memory holds on a question as one ready, ranked context pack.',
			inputSchema: { query: z.string().min(1), budget: z.number().int().positive().optional() }
		},
		async ({ query, budget }) => json(await recall(query, user, 8, budget))
	);

	server.registerTool(
		'remember',
		{
			description: 'Remember a piece of text as working memory, the cheap capture recall reads.',
			inputSchema: { text: z.string().min(1), kind: z.string().default('note') }
		},
		async ({ text, kind }) => json(await remember(text, user, kind))
	);

	server.registerTool(
		'reference',
		{
			description: 'Record a reference to a paper, url, or file so it is recallable later.',
			inputSchema: { uri: z.string().min(1) }
		},
		async ({ uri }) => json(await reference(uri, user))
	);

	server.registerTool(
		'share',
		{
			description: 'Share visible notes into one scope set as provenance-linked copies.',
			inputSchema: { documents: z.array(z.guid()), scopes: z.array(z.guid()) }
		},
		async ({ documents, scopes }) => json(await share(documents, scopes, user))
	);

	return server;
};

const auth = LogtoAuth.fromEnv();

const port = Number(env.PORT ?? 3112);
serve({
	port,
	fetch: async (request: Request): Promise<Response> => {
		try {
			const url = new URL(request.url);
			if (url.pathname === '/health') return Response.json({ status: 'ok' });
			if (auth !== undefined && url.pathname === auth.metadataPath) {
				return auth.metadata(request.method);
			}
			if (url.pathname !== '/mcp') return new Response('not found', { status: 404 });
			let user = stubUser();
			if (auth !== undefined) {
				const caller = await auth.authenticate(request);
				if (caller instanceof Response) return caller;
				user = caller;
			}
			// Stateless streamable HTTP: one fresh transport and server pair per request.
			const transport = new WebStandardStreamableHTTPServerTransport({
				sessionIdGenerator: undefined
			});
			await buildServer(user).connect(transport);
			return await transport.handleRequest(request);
		} catch (error) {
			console.error('mcp request failed:', error);
			return new Response(String(error), { status: 500 });
		}
	}
});
console.log(
	`aizk mcp (ts) listening on :${port}/mcp (${auth === undefined ? 'dev stub identity' : `logto resource ${auth.resource}`})`
);
