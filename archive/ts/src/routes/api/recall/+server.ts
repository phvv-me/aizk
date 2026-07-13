// POST /api/recall { query, k?, budget? } → the context pack, the TS twin of the MCP verb.
// Identity is a development stub until the Logto hook lands: the caller names its scopes.
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

import { recall } from '$lib/server/recall/recall';

export const POST: RequestHandler = async ({ request }) => {
	const body = await request.json();
	const user = {
		id: body.user ?? '00000000-0000-0000-0000-000000000001',
		label: body.label,
		read: body.scopes ?? [body.user ?? '00000000-0000-0000-0000-000000000001'],
		write: body.scopes ?? [body.user ?? '00000000-0000-0000-0000-000000000001'],
		public: []
	};
	return json(await recall(body.query, user, body.k ?? 8, body.budget));
};
