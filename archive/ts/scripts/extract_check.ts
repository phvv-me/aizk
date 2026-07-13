// Twin-database parity harness for the ported extraction pipeline. The Python driver
// (scratchpad extract_py.py) runs the real aizk build against aizk_extract_py first; this
// script runs the TS port against aizk_extract_ts for the same phase, then dumps both
// databases through the admin role, normalizes the volatile columns (claim row ids,
// timestamps, the document-created-at valid fallback, retraction stamps), diffs table by
// table, cross-checks the pure functions against the Python report, and prints one PARITY
// table. Usage: tsx scripts/extract_check.ts phase1|phase2 <fixture.json>
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { argv, exit } from 'node:process';

import postgres from 'postgres';

import { client } from '../src/lib/server/db';
import { parseDate } from '../src/lib/server/extract/dating';
import { casefold } from '../src/lib/server/extract/grounding';
import { ingestText, ingestTexts } from '../src/lib/server/extract/ingest';
import type { CaptureContext } from '../src/lib/server/extract/models';
import { emptyCapture } from '../src/lib/server/extract/models';
import { buildGraph } from '../src/lib/server/graph/build';
import { entityId, factId } from '../src/lib/server/graph/ids';
import { normalizeName } from '../src/lib/server/graph/naming';

interface FixtureDocument {
	title: string;
	source_uri: string | null;
	capture: Record<string, string> | null;
	text: string;
}

interface Fixture {
	created_by: string;
	scopes: string[];
	documents: FixtureDocument[];
	refresh: { title: string; text: string };
	casefold_cases: string[];
	naming_cases: string[];
	dating_cases: string[];
}

const phase = argv[2];
const fixturePath = argv[3];
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as Fixture;
const user = {
	id: fixture.created_by,
	read: fixture.scopes,
	write: fixture.scopes,
	public: []
};

const captureFor = (doc: FixtureDocument): CaptureContext | null =>
	doc.capture === null
		? null
		: {
				...emptyCapture(),
				speakerLabel: doc.capture.speaker_label ?? null,
				speakerRole: doc.capture.speaker_role ?? null,
				observedAt: doc.capture.observed_at ?? null
			};

const runPhase1 = async (): Promise<void> => {
	const ids = await ingestTexts(
		fixture.documents.map((doc) => ({
			text: doc.text,
			title: doc.title,
			kind: 'note',
			sourceUri: doc.source_uri,
			createdBy: fixture.created_by,
			scopes: fixture.scopes,
			capture: captureFor(doc)
		})),
		user
	);
	console.log('ingested', ids);
	console.log('phase1 build', await buildGraph({ scopes: fixture.scopes }));
};

const runPhase2 = async (): Promise<void> => {
	const target = fixture.documents.find((doc) => doc.title === fixture.refresh.title);
	if (target === undefined) throw new Error('refresh target not in fixture');
	const documentId = await ingestText(fixture.refresh.text, user, {
		title: fixture.refresh.title,
		kind: 'note',
		sourceUri: target.source_uri,
		createdBy: fixture.created_by,
		scopes: fixture.scopes,
		capture: captureFor(target)
	});
	console.log('refreshed', documentId);
	console.log('phase2 build', await buildGraph({ scopes: fixture.scopes }));
};

// The normalized dump statements, identical for both databases and restricted to the
// fixture's exact scope set so rows baked into (or concurrently written to) the aizk_ts
// template never enter the diff. Volatile columns stay out: claim row uuids, recorded
// bounds, retraction stamp values; the document-created-at valid fallback compares with
// millisecond tolerance because JS Dates carry no microseconds.
const scopesLiteral = `'{${fixture.scopes.join(',')}}'::uuid[]`;
const DUMPS: Record<string, string> = {
	chunk: `
		select d.title as doc, c.ord, c.text, c.lexical, c.provenance::text as provenance,
			md5(c.embedding::text) as embedding, (c.processed_at is not null) as processed
		from chunk c join document d on d.id = c.document_id
		where c.scopes = ${scopesLiteral}
		order by d.title, c.ord`,
	entity_content: `
		select id, name, type, md5(embedding::text) as embedding
		from entity_content
		where id in (select content_id from entity_claim where scopes = ${scopesLiteral})
		order by id`,
	fact_content: `
		select id, subject_id, object_id, predicate, statement, md5(embedding::text) as embedding
		from fact_content
		where id in (select content_id from fact_claim where scopes = ${scopesLiteral})
		order by id`,
	entity_claim: `
		select content_id, array_to_string(scopes, ',') as scopes, created_by, attributes::text as attributes
		from entity_claim where scopes = ${scopesLiteral} order by content_id`,
	fact_claim: `
		with normalized as (
			select fc.content_id, array_to_string(fc.scopes, ',') as scopes, fc.perspective_key,
				upper_inf(fc.recorded) as live,
				case
					when fc.valid is null then null
					when exists (
						select 1 from document d2
						where abs(extract(epoch from (lower(fc.valid) - d2.created_at))) < 0.001
					) then '[DOC_CREATED_AT,' || coalesce(upper(fc.valid)::text, '') || ')'
					else fc.valid::text
				end as valid_norm,
				(fc.attributes - 'source_refreshed' - 'forgotten' - 'decayed')::text as attributes,
				(fc.attributes ? 'source_refreshed') as refreshed,
				d.title as source_doc, c.ord as source_ord, fc.created_by
			from fact_claim fc
			left join chunk c on c.id = fc.source_chunk_id
			left join document d on d.id = c.document_id
			where fc.scopes = ${scopesLiteral}
		)
		select * from normalized
		order by content_id, perspective_key, live, refreshed, coalesce(valid_norm, ''),
			coalesce(source_doc, ''), coalesce(source_ord, -1)`
};

interface TableVerdict {
	table: string;
	py: number;
	ts: number;
	ok: boolean;
	detail: string;
}

const admin = (db: string): postgres.Sql =>
	postgres(`postgresql://aizk_admin:aizk@localhost:5433/${db}`, { prepare: false, max: 1 });

const compareTables = async (): Promise<TableVerdict[]> => {
	const py = admin('aizk_extract_py');
	const ts = admin('aizk_extract_ts');
	const verdicts: TableVerdict[] = [];
	for (const [table, statement] of Object.entries(DUMPS)) {
		const pyRows = (await py.unsafe(statement)) as unknown as Record<string, unknown>[];
		const tsRows = (await ts.unsafe(statement)) as unknown as Record<string, unknown>[];
		let detail = '';
		let ok = pyRows.length === tsRows.length;
		for (let index = 0; index < Math.max(pyRows.length, tsRows.length); index += 1) {
			const a = JSON.stringify(pyRows[index] ?? null);
			const b = JSON.stringify(tsRows[index] ?? null);
			if (a !== b) {
				ok = false;
				detail = `row ${index}\n      py: ${a}\n      ts: ${b}`;
				break;
			}
		}
		verdicts.push({ table, py: pyRows.length, ts: tsRows.length, ok, detail });
	}
	await py.end();
	await ts.end();
	return verdicts;
};

interface PyReport {
	casefold: Record<string, string>;
	normalize_name: Record<string, string>;
	ids: Record<string, string>;
	dating: Record<string, string | null>;
}

// Cross-check the pure ported functions against the Python report and return flagged
// divergences; dating divergences are the documented dateparser seam.
const functionChecks = (report: PyReport): { agreements: number; flags: string[] } => {
	const flags: string[] = [];
	let agreements = 0;
	for (const [input, expected] of Object.entries(report.casefold)) {
		if (casefold(input) === expected) agreements += 1;
		else flags.push(`casefold(${JSON.stringify(input)}): py=${expected} ts=${casefold(input)}`);
	}
	for (const [input, expected] of Object.entries(report.normalize_name)) {
		const got = normalizeName(input);
		if (got === expected) agreements += 1;
		else flags.push(`normalize_name(${JSON.stringify(input)}): py=${JSON.stringify(expected)} ts=${JSON.stringify(got)}`);
	}
	const tsIds: Record<string, string> = {
		'entity Alpha Project/project': entityId('Alpha Project', 'project'),
		'entity The alpha/concept': entityId('The alpha', 'concept'),
		'fact sample': factId(
			'The alpha',
			'related_to',
			'project tracks',
			'The alpha related_to project tracks.'
		)
	};
	for (const [name, expected] of Object.entries(report.ids)) {
		if (tsIds[name] === expected) agreements += 1;
		else flags.push(`ids ${name}: py=${expected} ts=${tsIds[name]}`);
	}
	for (const [input, expected] of Object.entries(report.dating)) {
		const got = parseDate(input);
		const gotInstant = got === null ? null : got.getTime();
		const expectedInstant = expected === null ? null : new Date(expected).getTime();
		if (gotInstant === expectedInstant) agreements += 1;
		else
			flags.push(
				`DATING SEAM parseDate(${JSON.stringify(input)}): py=${expected} ts=${got === null ? null : got.toISOString()}`
			);
	}
	return { agreements, flags };
};

const main = async (): Promise<void> => {
	if (phase === 'phase1') await runPhase1();
	else if (phase === 'phase2') await runPhase2();
	else throw new Error(`unknown phase ${phase}`);
	await client.end();

	const verdicts = await compareTables();
	console.log(`\nPARITY ${phase}`);
	console.log('table            py rows  ts rows  verdict');
	for (const verdict of verdicts) {
		const status = verdict.ok ? 'OK' : 'DIVERGES';
		console.log(
			`${verdict.table.padEnd(16)} ${String(verdict.py).padStart(7)}  ${String(verdict.ts).padStart(7)}  ${status}`
		);
		if (verdict.detail) console.log(`    ${verdict.detail}`);
	}

	let flagged: string[] = [];
	if (phase === 'phase1') {
		const report = JSON.parse(
			readFileSync(join(dirname(fixturePath), 'py_report.json'), 'utf8')
		) as PyReport;
		const { agreements, flags } = functionChecks(report);
		flagged = flags;
		console.log(`\nfunction cross-checks: ${agreements} agree, ${flags.length} flagged`);
		for (const flag of flags) console.log(`  ${flag}`);
	}

	const tablesOk = verdicts.every((verdict) => verdict.ok);
	const hardFlags = flagged.filter((flag) => !flag.startsWith('DATING SEAM'));
	console.log(
		tablesOk && !hardFlags.length
			? `\nPARITY ${phase}: PASS${flagged.length ? ' (dating seam divergences documented above)' : ''}`
			: `\nPARITY ${phase}: FAIL`
	);
	exit(tablesOk && !hardFlags.length ? 0 : 1);
};

await main();
