// Smoke-check the share verb against a live database: first call copies the document,
// its chunks, and the touched claims into the target scope, the second call is idempotent.
import { exit } from 'node:process';

import { sql } from 'drizzle-orm';

import { actingAs, type User } from '../src/lib/server/db';
import { share } from '../src/lib/server/promote';

const OWNER = process.env.AIZK_DEFAULT_USER ?? '00000000-0000-0000-0000-000000000001';
const TARGET = process.env.AIZK_SHARE_TARGET ?? '00000000-0000-0000-0000-00000000b0b0';

const user: User = { id: OWNER, read: [OWNER, TARGET], write: [OWNER, TARGET], public: [] };

const rows = async (query: ReturnType<typeof sql>): Promise<Record<string, unknown>[]> =>
	actingAs(user, async (tx) => (await tx.execute(query)) as Record<string, unknown>[]);

// Seed one document with two chunks and one live fact so every copy path is exercised.
// Content ids are precomputed like the Python writer's uuid5 identity: content rows are
// only readable once a claim names them, so a SELECT-back inside the same snapshot fails.
const seed = async (owned: string): Promise<void> =>
	actingAs(user, async (tx) => {
		const [documentId, chunkId, subjectId, objectId, factId] = Array.from(
			{ length: 5 },
			() => crypto.randomUUID()
		);
		await tx.execute(sql`
			insert into document (id, kind, title, content_hash, created_by, scopes)
			values (${documentId}, 'note', 'share fixture', md5(random()::text), ${OWNER}, ${owned}::uuid[])
		`);
		await tx.execute(sql`
			insert into chunk (id, document_id, ord, text, created_by, scopes)
			select case ord when 0 then ${chunkId}::uuid else uuidv7() end, ${documentId}::uuid,
				ord, 'fixture chunk ' || ord, ${OWNER}, ${owned}::uuid[]
			from generate_series(0, 1) ord
		`);
		await tx.execute(sql`
			insert into entity_content (id, name, type)
			select unnest(array[${subjectId}, ${objectId}]::uuid[]),
				unnest(array['fixture subject', 'fixture object']),
				(select name from entity_kind where not structural limit 1)
		`);
		await tx.execute(sql`
			insert into entity_claim (id, content_id, created_by, scopes)
			select uuidv7(), unnest(array[${subjectId}, ${objectId}]::uuid[]), ${OWNER}, ${owned}::uuid[]
		`);
		await tx.execute(sql`
			insert into fact_content (id, subject_id, object_id, predicate, statement)
			values (${factId}, ${subjectId}, ${objectId},
				(select name from relation_kind where not structural limit 1),
				'fixture subject relates to fixture object')
		`);
		await tx.execute(sql`
			insert into fact_claim (id, content_id, source_chunk_id, created_by, scopes)
			values (uuidv7(), ${factId}, ${chunkId}, ${OWNER}, ${owned}::uuid[])
		`);
	});

const main = async () => {
	const owned = `{${OWNER}}`;
	const shared = `{${TARGET}}`;
	const sourceQuery = sql`select d.id, count(c.id)::int as chunks
		from document d join chunk c on c.document_id = d.id
		where d.scopes = ${owned}::uuid[] group by d.id order by chunks desc limit 1`;
	let [source] = await rows(sourceQuery);
	if (!source) {
		await seed(owned);
		[source] = await rows(sourceQuery);
	}

	const first = await share([source.id as string], [TARGET], user);
	const second = await share([source.id as string], [TARGET], user);
	const copies = await rows(
		sql`select id from document where promoted_from = ${source.id as string} and scopes = ${shared}::uuid[]`
	);
	const [copied] = await rows(
		sql`select count(*)::int as chunks from chunk where document_id = ${copies[0]?.id as string}`
	);
	const [claims] = await rows(
		sql`select
			(select count(*)::int from entity_claim where scopes = ${shared}::uuid[]) as entities,
			(select count(*)::int from fact_claim where scopes = ${shared}::uuid[]) as facts`
	);
	console.log(JSON.stringify({ first, second, copies: copies.length, source, copied, claims }));
	exit(0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
