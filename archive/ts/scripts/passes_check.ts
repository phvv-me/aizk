// Smoke-check the graph maintenance passes against a live database: seed one disposable
// scope with two fact triangles, duplicate and path-like entities, a stale claim, and aged
// session items, then run every pass with deterministic injected reporters and the mock
// embedder, rolling the reembed rewrites back and deleting the fixture scope afterwards.
import { exit } from 'node:process';

process.env.AIZK_DB_NAME ??= 'aizk_ts';
process.env.AIZK_EMBED_URL ??= 'http://127.0.0.1:8090/v1';
// Force the rollup path: a two-community tree only grows a parent below root max one.
process.env.AIZK_RAPTOR_ROOT_MAX ??= '1';
process.env.AIZK_RAPTOR_SIM_THRESHOLD ??= '-1';

const { sql } = await import('drizzle-orm');
const { embed } = await import('../src/lib/server/serving');
const { asSystem, bypassRls, scopeKey, uuidArray } = await import('../src/lib/server/graph/system');
const { buildCommunities } = await import('../src/lib/server/graph/communities');
const { buildRaptor } = await import('../src/lib/server/graph/raptor');
const { buildProfile, refreshProfiles } = await import('../src/lib/server/graph/profiles');
const { deriveInsights } = await import('../src/lib/server/graph/insight');
const { promoteSessions } = await import('../src/lib/server/graph/session_tier');
const { dedupEntities } = await import('../src/lib/server/graph/repair');
const { decay } = await import('../src/lib/server/graph/decay');
const { EMBEDDED_TARGETS, rewriteEmbeddings } = await import('../src/lib/server/graph/reembed');

const SMOKE = process.env.AIZK_PASSES_SCOPE ?? '00000000-0000-0000-0000-00000000cafe';
const key = scopeKey([SMOKE]);
const scope = uuidArray(key);

const label = (user: string): string => `fixture theme ${user.length % 97}`;
const summary = (user: string): string =>
	`Fixture summary of ${user.replaceAll('\n', ' ').slice(0, 120)}`;

// Two triangles of related entities, a duplicate pair, and a path-like name, all claimed
// only in the smoke scope so every pass touches nothing outside it.
const NAMES = [
	'passes fixture alpha one',
	'passes fixture alpha two',
	'passes fixture alpha three',
	'passes fixture beta one',
	'passes fixture beta two',
	'passes fixture beta three',
	'Passes Fixture Dup',
	'passes  fixture   DUP!',
	'/tmp/passes fixture path'
];
const entityIds = NAMES.map(() => crypto.randomUUID());
const triangle = (offset: number): [number, number][] => [
	[offset, offset + 1],
	[offset + 1, offset + 2],
	[offset + 2, offset]
];
const edges: [number, number][] = [...triangle(0), ...triangle(3), [6, 7], [8, 0]];

const seed = async (): Promise<{ factIds: string[]; sessionIds: string[] }> => {
	const statements = edges.map(([a, b]) => `${NAMES[a]} relates to ${NAMES[b]} in the fixture`);
	const vectors = await embed(statements, 'document');
	const factIds = edges.map(() => crypto.randomUUID());
	const staleFactId = crypto.randomUUID();
	const sessionIds = [crypto.randomUUID(), crypto.randomUUID()];
	await asSystem(key, async (tx) => {
		const [kind] = (await tx.execute(
			sql`select name from entity_kind where not structural order by name limit 1`
		)) as unknown as { name: string }[];
		const [relation] = (await tx.execute(
			sql`select name from relation_kind where not structural order by name limit 1`
		)) as unknown as { name: string }[];
		for (const [index, name] of NAMES.entries()) {
			await tx.execute(sql`
				insert into entity_content (id, name, type) values (${entityIds[index]}, ${name}, ${kind.name})
			`);
			await tx.execute(sql`
				insert into entity_claim (id, content_id, created_by, scopes)
				values (uuidv7(), ${entityIds[index]}, ${key[0]}, ${scope}::uuid[])
			`);
		}
		for (const [index, [a, b]] of edges.entries()) {
			await tx.execute(sql`
				insert into fact_content (id, subject_id, object_id, predicate, statement, embedding)
				values (${factIds[index]}, ${entityIds[a]}, ${entityIds[b]}, ${relation.name},
					${statements[index]}, ${vectors[index]}::halfvec)
			`);
			await tx.execute(sql`
				insert into fact_claim (id, content_id, created_by, scopes)
				values (uuidv7(), ${factIds[index]}, ${key[0]}, ${scope}::uuid[])
			`);
		}
		await tx.execute(sql`
			insert into fact_content (id, subject_id, object_id, predicate, statement)
			values (${staleFactId}, ${entityIds[0]}, null, ${relation.name}, 'passes fixture stale fact')
		`);
		await tx.execute(sql`
			insert into fact_claim (id, content_id, created_by, scopes, last_accessed)
			values (uuidv7(), ${staleFactId}, ${key[0]}, ${scope}::uuid[], now() - interval '400 days')
		`);
		for (const [index, id] of sessionIds.entries())
			await tx.execute(sql`
				insert into session_item (id, kind, text, created_by, scopes, created_at)
				values (${id}, 'note', ${'passes fixture working item ' + index}, ${key[0]},
					${scope}::uuid[], now() - interval '2 hours')
			`);
	});
	return { factIds: [...factIds, staleFactId], sessionIds };
};

const cleanup = async (factIds: string[]): Promise<void> =>
	bypassRls(async (tx) => {
		await tx.execute(sql`delete from fact_claim where scopes = ${scope}::uuid[]`);
		await tx.execute(sql`delete from entity_claim where scopes = ${scope}::uuid[]`);
		await tx.execute(sql`delete from community where scopes = ${scope}::uuid[]`);
		await tx.execute(sql`delete from profile where scopes = ${scope}::uuid[]`);
		await tx.execute(sql`delete from session_item where scopes = ${scope}::uuid[]`);
		await tx.execute(sql`
			delete from fact_content where id = any(${uuidArray(factIds)}::uuid[])
				or (predicate in ('part_of', 'observes')
					and not exists (select 1 from fact_claim c where c.content_id = fact_content.id))
		`);
		await tx.execute(sql`
			delete from entity_content where id = any(${uuidArray(entityIds)}::uuid[])
				or (type in ('raptor_summary', 'observation')
					and not exists (select 1 from entity_claim c where c.content_id = entity_content.id))
		`);
	});

class Rollback extends Error {}

const main = async (): Promise<void> => {
	const { factIds } = await seed();
	try {
		const results: Record<string, unknown> = {};
		const report = async (system: string, user: string): Promise<{ label: string; summary: string }> => ({
			label: label(user),
			summary: summary(user)
		});
		results.communities = await buildCommunities(key, report);
		results.raptor = await buildRaptor(key, report);
		const profile = async (_system: string, user: string): Promise<{ summary: string }> => ({
			summary: summary(user)
		});
		results.profiles = await refreshProfiles(key, profile);
		results.profile = Boolean(await buildProfile(entityIds[0], key, profile));
		const reflect = async (): Promise<{ observations: { statement: string; significance: number }[] }> => ({
			observations: [
				{ statement: 'passes fixture triangles alpha and beta form two clusters', significance: 0.9 },
				{ statement: 'passes fixture low-value restatement', significance: 0.1 }
			]
		});
		results.insights = await deriveInsights(key, reflect);
		results.insightsAgain = await deriveInsights(key, reflect);
		let ingested = 0;
		const ingest = async (items: { id: string }[]): Promise<void> => {
			ingested = items.length;
		};
		results.promoted = await promoteSessions(ingest, key);
		results.ingested = ingested;
		results.promotedAgain = await promoteSessions(ingest, key);
		results.deduped = await dedupEntities(key);
		results.decayed = await decay(key);
		// Prove every reembed rewrite runs, then roll the whole rewrite back so the stored
		// vectors keep their real values.
		const tables = ['chunk', 'community', 'profile', 'entity_content', 'fact_content'];
		const reembedded: Record<string, number> = {};
		try {
			await bypassRls(async (tx) => {
				for (const [index, target] of EMBEDDED_TARGETS.entries())
					reembedded[tables[index]] = await rewriteEmbeddings(
						tx,
						target,
						target.scoped ? key : undefined
					);
				throw new Rollback('roll the rewrites back');
			});
		} catch (error) {
			if (!(error instanceof Rollback)) throw error;
		}
		results.reembedded = reembedded;
		console.log(JSON.stringify(results));
	} finally {
		await cleanup(factIds);
	}
	exit(0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
