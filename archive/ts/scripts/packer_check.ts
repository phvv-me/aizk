// Prove the TS packer twin: for every fixture case, the unpacked statement plus the pack
// walk must reproduce the packed statement's kept rows and used_tokens exactly.
import { readFileSync } from 'node:fs';
import { exit } from 'node:process';

import { actingAs, type User } from '../src/lib/server/db';
import { buildRecallStatement, type Route } from '../src/lib/server/recall/query';
import { pack, type CandidateRow } from '../src/lib/server/recall/recall';

interface FixtureCase {
	topic: number;
	route: Route;
	vector: string;
	mentions: string[];
}

class Rollback extends Error {}

const cases = JSON.parse(readFileSync(process.argv[2], 'utf-8')) as FixtureCase[];
const user: User = {
	id: process.env.AIZK_BENCH_USER ?? '',
	read: [process.env.AIZK_BENCH_USER ?? ''],
	write: [process.env.AIZK_BENCH_USER ?? ''],
	public: []
};

const run = async (statement: ReturnType<typeof buildRecallStatement>): Promise<unknown[]> => {
	let rows: unknown[] = [];
	await actingAs(user, async (tx) => {
		rows = (await tx.execute(statement)) as unknown[];
		throw new Rollback();
	}).catch((error) => {
		if (!(error instanceof Rollback)) throw error;
	});
	return rows;
};

const main = async () => {
	let failures = 0;
	for (const item of cases) {
		const params = {
			vector: item.vector,
			text: 'topic keyword query',
			mentions: item.mentions,
			k: 8,
			budget: 2048
		};
		const packedRows = (await run(buildRecallStatement(item.route, params))) as Record<
			string,
			unknown
		>[];
		const candidates = (await run(
			buildRecallStatement(item.route, params, false)
		)) as unknown as CandidateRow[];
		const [kept, used] = pack(candidates, params.budget);
		const wantTokens = packedRows.reduce(
			(most, row) => Math.max(most, row.used_tokens as number),
			0
		);
		const shape = (row: Record<string, unknown>) =>
			JSON.stringify([row.lane, row.line, row.source_chunk_id, row.created_by]);
		const same =
			kept.length === packedRows.length &&
			used === wantTokens &&
			kept.every((row, index) => shape(row as never) === shape(packedRows[index]));
		if (!same) {
			failures += 1;
			console.log(
				`MISMATCH topic ${item.topic} ${item.route}: sql ${packedRows.length} rows/${wantTokens} tokens, twin ${kept.length} rows/${used} tokens`
			);
		} else {
			console.log(`ok   topic ${item.topic} ${item.route} (${kept.length} rows, ${used} tokens)`);
		}
	}
	console.log(failures ? `${failures} MISMATCHES` : 'PACKER TWIN OK');
	exit(failures ? 1 : 0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
