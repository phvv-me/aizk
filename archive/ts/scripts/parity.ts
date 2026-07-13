// Execute the ported recall statement for every fixture case against aizk_bench inside a
// rolled-back transaction and diff each packed row against the Python reference output.
import { readFileSync } from 'node:fs';
import { exit } from 'node:process';

import { actingAs, type User } from '../src/lib/server/db';
import { buildRecallStatement, type Route } from '../src/lib/server/recall/query';

interface FixtureRow {
	lane: string;
	line: string;
	source_chunk_id: string | null;
	source_title: string | null;
	source_uri: string | null;
	created_by: string | null;
	used_tokens: number;
}

interface FixtureCase {
	topic: number;
	route: Route;
	vector: string;
	mentions: string[];
	rows: FixtureRow[];
}

class Rollback extends Error {}

const fixturePath = process.argv[2];
const cases = JSON.parse(readFileSync(fixturePath, 'utf-8')) as FixtureCase[];
const benchUser = JSON.parse(
	readFileSync(fixturePath.replace('parity_fixture', 'bench_manifest'), 'utf-8')
).user as string;
const user: User = { id: benchUser, read: [benchUser], write: [benchUser], public: [] };

let failures = 0;
for (const parityCase of cases) {
	const statement = buildRecallStatement(parityCase.route, {
		vector: parityCase.vector,
		text: 'topic keyword query',
		mentions: parityCase.mentions,
		k: 8,
		budget: 2048
	});
	let rows: FixtureRow[] = [];
	try {
		await actingAs(user, async (tx) => {
			rows = (await tx.execute(statement)) as unknown as FixtureRow[];
			throw new Rollback();
		});
	} catch (error) {
		if (!(error instanceof Rollback)) throw error;
	}
	const got = rows.map((row) => ({ ...row, used_tokens: Number(row.used_tokens) }));
	const label = `topic ${parityCase.topic} ${parityCase.route}`;
	if (JSON.stringify(got) === JSON.stringify(parityCase.rows)) {
		console.log(`ok   ${label} (${got.length} rows)`);
		continue;
	}
	failures += 1;
	console.log(`FAIL ${label}: ts=${got.length} rows, python=${parityCase.rows.length} rows`);
	for (let index = 0; index < Math.max(got.length, parityCase.rows.length); index += 1) {
		const ts = JSON.stringify(got[index] ?? null);
		const py = JSON.stringify(parityCase.rows[index] ?? null);
		if (ts !== py) {
			console.log(`  row ${index}\n    ts: ${ts?.slice(0, 220)}\n    py: ${py?.slice(0, 220)}`);
			break;
		}
	}
}
console.log(failures === 0 ? 'PARITY OK' : `${failures}/${cases.length} cases diverge`);
exit(failures === 0 ? 0 : 1);
