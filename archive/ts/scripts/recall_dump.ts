// Dump recall ContextPacks as JSON for parity comparison against the Python server.
import { exit, stdout } from 'node:process';

import type { User } from '../src/lib/server/db';
import { recall } from '../src/lib/server/recall/recall';

const owner = process.env.AIZK_BENCH_USER ?? '';
const user: User = { id: owner, read: [owner], write: [owner], public: [] };

const main = async () => {
	const queries = process.argv.slice(2);
	const packs = [];
	for (const query of queries) packs.push(await recall(query, user));
	stdout.write(JSON.stringify(packs, null, 1));
	exit(0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
