// Time the 30 parity-case statements through the TS stack, db-only.
import { readFileSync } from 'node:fs';

import { actingAs, type User } from '../src/lib/server/db';
import { buildRecallStatement, type Route } from '../src/lib/server/recall/query';

class Rollback extends Error {}

const scratch = process.argv[2];
const cases = JSON.parse(readFileSync(`${scratch}/parity_fixture.json`, 'utf-8')) as {
	route: Route;
	vector: string;
	mentions: string[];
}[];
const benchUser = JSON.parse(readFileSync(`${scratch}/bench_manifest.json`, 'utf-8')).user as string;
const user: User = { id: benchUser, read: [benchUser], write: [benchUser], public: [] };

const samples: number[] = [];
for (const parityCase of cases) {
	const statement = buildRecallStatement(parityCase.route, {
		vector: parityCase.vector,
		text: 'topic keyword query',
		mentions: parityCase.mentions,
		k: 8,
		budget: 2048
	});
	const start = performance.now();
	try {
		await actingAs(user, async (tx) => {
			await tx.execute(statement);
			throw new Rollback();
		});
	} catch (error) {
		if (!(error instanceof Rollback)) throw error;
	}
	samples.push(performance.now() - start);
}
samples.sort((a, b) => a - b);
const total = samples.reduce((sum, value) => sum + value, 0);
console.log(
	`ts:     p50=${samples[Math.floor(samples.length / 2)].toFixed(1)}ms total=${total.toFixed(0)}ms n=${samples.length}`
);
process.exit(0);
