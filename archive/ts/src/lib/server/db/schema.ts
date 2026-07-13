// The aizk schema in drizzle, ported table for table from the Python store models and the
// alembic-created database. Authority is the same scope lattice: every scoped row carries a
// nonempty `scopes uuid[]`, and the policies below compile the identical conditions the
// Python `rls` package emits, reading the caller's standing from the `app.scopes` GUC.
// FORCE ROW LEVEL SECURITY, extensions, the bm25 trigger, and the ontology seeds live in
// the custom SQL migration; drizzle-kit owns everything declarative here.
import { sql, type SQL } from 'drizzle-orm';
import {
	boolean,
	customType,
	bigint,
	index,
	integer,
	jsonb,
	pgEnum,
	pgPolicy,
	pgRole,
	pgTable,
	pgView,
	text,
	timestamp,
	unique,
	uniqueIndex,
	uuid,
	varchar,
	type AnyPgColumn
} from 'drizzle-orm/pg-core';

export const appRole = pgRole('aizk_app').existing();

const halfvec = customType<{ data: string; config: { dim: number } }>({
	dataType: (config) => `halfvec(${config?.dim ?? 1024})`
});
const tstzrange = customType<{ data: string }>({ dataType: () => 'tstzrange' });
const tsvector = customType<{ data: string }>({ dataType: () => 'tsvector' });
const bm25vector = customType<{ data: string }>({ dataType: () => 'bm25vector' });

export const watermarkKind = pgEnum('watermark_kind', [
	'entity_dirty',
	'fact_count',
	'raptor_fact_count',
	'scorecard'
]);

// Column groups shared by every table family, the Python mixins as spreads.
const timestamps = {
	createdAt: timestamp('created_at', { withTimezone: true }).defaultNow().notNull(),
	updatedAt: timestamp('updated_at', { withTimezone: true }).defaultNow().notNull()
};
const scoped = {
	createdBy: uuid('created_by').notNull(),
	scopes: uuid('scopes')
		.array()
		.default(sql`'{}'::uuid[]`)
		.notNull()
};
const id = { id: uuid('id').primaryKey() };

// The caller's standing for one permission, the exact expression the Python rls package
// compiles: the app.scopes GUC is JSON with read/write/public arrays of scope ids.
const authority = (permission: string): SQL => {
	const standing = `(NULLIF(current_setting('app.scopes', true), ''))::jsonb -> '${permission}'`;
	const array = 'ARRAY(SELECT (value)::uuid FROM jsonb_array_elements_text(' + standing + '))';
	return sql.raw(array);
};

interface ScopedPolicies {
	mutable?: boolean;
	deletable?: boolean;
	readThrough?: string;
}

// The scope-lattice policies every scoped table shares: complete standing in the row's
// scope intersection for writes, readable or single-public standing for reads, and a
// read-through variant that delegates visibility to the parent table's own policies.
const scopedPolicies = (
	table: { scopes: AnyPgColumn },
	flags: ScopedPolicies = {}
): ReturnType<typeof pgPolicy>[] => {
	const scopes = table.scopes;
	const nonempty = sql`cardinality(${scopes}) > 0`;
	const write = sql`${nonempty} AND ${scopes} <@ ${authority('write')}`;
	const parentVisible = `${flags.readThrough}_id IN (SELECT id FROM ${flags.readThrough})`;
	const read = flags.readThrough
		? sql.raw(parentVisible)
		: sql`${nonempty} AND (${scopes} <@ ${authority('read')}
			OR (cardinality(${scopes}) = 1 AND ${scopes} <@ ${authority('public')}))`;
	const policies = [
		pgPolicy('scope_read', { for: 'select', to: appRole, using: read }),
		pgPolicy('scope_insert', { for: 'insert', to: appRole, withCheck: write })
	];
	if (flags.mutable)
		policies.push(pgPolicy('scope_update', { for: 'update', to: appRole, using: write, withCheck: write }));
	if (flags.deletable) policies.push(pgPolicy('scope_delete', { for: 'delete', to: appRole, using: write }));
	return policies;
};

// Content tables are immutable and shared: a row is visible when any visible claim names
// it, and anyone may mint content because claims are the only authority carrier.
const contentPolicies = (claimTable: string): ReturnType<typeof pgPolicy>[] => [
	pgPolicy('content_read', {
		for: 'select',
		to: appRole,
		using: sql.raw('id IN (SELECT content_id FROM ' + claimTable + ')')
	}),
	pgPolicy('content_insert', { for: 'insert', to: appRole, withCheck: sql`true` })
];

export const document = pgTable(
	'document',
	{
		...timestamps,
		...scoped,
		...id,
		kind: varchar('kind').notNull(),
		title: varchar('title'),
		sourceUri: varchar('source_uri'),
		contentHash: varchar('content_hash').notNull(),
		promotedFrom: uuid('promoted_from').references((): AnyPgColumn => document.id)
	},
	(t) => [
		index('ix_document_created_by').on(t.createdBy),
		index('ix_document_content_hash').on(t.contentHash),
		index('ix_document_scopes').using('gin', t.scopes),
		unique('uq_document_source_scope').on(t.sourceUri, t.scopes),
		...scopedPolicies(t, { mutable: true })
	]
);

export const chunk = pgTable(
	'chunk',
	{
		embedding: halfvec('embedding', { dim: 1024 }),
		...scoped,
		...id,
		documentId: uuid('document_id')
			.notNull()
			.references(() => document.id, { onDelete: 'cascade' }),
		ord: integer('ord').notNull(),
		text: text('text').notNull(),
		lexical: text('lexical'),
		tokens: integer('tokens'),
		provenance: jsonb('provenance').default(sql`'{}'::jsonb`).notNull(),
		tsv: tsvector('tsv').generatedAlwaysAs(
			(): SQL => sql`to_tsvector('english', coalesce(${chunk.lexical}, ${chunk.text}))`
		),
		bm25: bm25vector('bm25'),
		processedAt: timestamp('processed_at', { withTimezone: true })
	},
	(t) => [
		index('ix_chunk_document_id').on(t.documentId),
		index('ix_chunk_created_by').on(t.createdBy),
		index('ix_chunk_tsv').using('gin', t.tsv),
		index('ix_chunk_scopes').using('gin', t.scopes),
		index('ix_chunk_pending').on(t.id).where(sql`processed_at IS NULL`),
		index('ix_chunk_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		...scopedPolicies(t, { mutable: true, deletable: true, readThrough: 'document' })
	]
);

export const entityContent = pgTable(
	'entity_content',
	{
		...id,
		name: text('name').notNull(),
		type: text('type')
			.notNull()
			.references(() => entityKind.name),
		embedding: halfvec('embedding', { dim: 1024 })
	},
	(t) => [
		index('ix_entity_content_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		index('ix_entity_content_name_lower').on(sql`lower(name)`),
		index('ix_entity_content_name_trgm').using('gin', sql`lower(name) gin_trgm_ops`),
		...contentPolicies('entity_claim')
	]
);

export const entityClaim = pgTable(
	'entity_claim',
	{
		...timestamps,
		...scoped,
		...id,
		contentId: uuid('content_id')
			.notNull()
			.references(() => entityContent.id, { onDelete: 'cascade' }),
		attributes: jsonb('attributes').default(sql`'{}'::jsonb`).notNull()
	},
	(t) => [
		index('ix_entity_claim_content_id').on(t.contentId),
		index('ix_entity_claim_created_by').on(t.createdBy),
		index('ix_entity_claim_scopes').using('gin', t.scopes),
		unique('uq_entity_claim_content_scope').on(t.contentId, t.scopes),
		...scopedPolicies(t)
	]
);

export const factContent = pgTable(
	'fact_content',
	{
		...id,
		subjectId: uuid('subject_id')
			.notNull()
			.references(() => entityContent.id, { onDelete: 'cascade' }),
		objectId: uuid('object_id').references(() => entityContent.id, { onDelete: 'cascade' }),
		predicate: text('predicate')
			.notNull()
			.references(() => relationKind.name),
		statement: text('statement').notNull(),
		embedding: halfvec('embedding', { dim: 1024 })
	},
	(t) => [
		index('ix_fact_content_subject_id').on(t.subjectId),
		index('ix_fact_content_object_id').on(t.objectId),
		index('ix_fact_content_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		...contentPolicies('fact_claim')
	]
);

export const factClaim = pgTable(
	'fact_claim',
	{
		...scoped,
		...id,
		contentId: uuid('content_id')
			.notNull()
			.references(() => factContent.id, { onDelete: 'cascade' }),
		valid: tstzrange('valid'),
		recorded: tstzrange('recorded')
			.default(sql`tstzrange(now(), NULL::timestamp with time zone, '[)'::text)`)
			.notNull(),
		lastAccessed: timestamp('last_accessed', { withTimezone: true }),
		accessCount: integer('access_count').default(0).notNull(),
		attributes: jsonb('attributes').default(sql`'{}'::jsonb`).notNull(),
		perspectiveKey: varchar('perspective_key').default('world').notNull(),
		sourceChunkId: uuid('source_chunk_id').references(() => chunk.id, { onDelete: 'set null' }),
		promotedFrom: uuid('promoted_from').references((): AnyPgColumn => factClaim.id, {
			onDelete: 'set null'
		})
	},
	(t) => [
		index('ix_fact_claim_content_id').on(t.contentId),
		index('ix_fact_claim_created_by').on(t.createdBy),
		index('ix_fact_claim_perspective_key').on(t.perspectiveKey),
		index('ix_fact_claim_source_chunk_id').on(t.sourceChunkId),
		index('ix_fact_claim_promoted_from').on(t.promotedFrom),
		index('ix_fact_claim_scopes').using('gin', t.scopes),
		index('ix_fact_claim_valid').using('gist', t.valid),
		index('ix_fact_claim_recorded').using('gist', t.recorded),
		index('ix_fact_claim_live').using('gist', t.valid).where(sql`upper_inf(recorded)`),
		uniqueIndex('uq_fact_claim_live')
			.on(t.contentId, t.scopes, t.perspectiveKey)
			.where(sql`upper_inf(recorded)`),
		...scopedPolicies(t, { mutable: true })
	]
);

export const community = pgTable(
	'community',
	{
		embedding: halfvec('embedding', { dim: 1024 }),
		...timestamps,
		...scoped,
		...id,
		label: text('label').notNull(),
		summary: text('summary').notNull(),
		memberIds: uuid('member_ids')
			.array()
			.default(sql`'{}'::uuid[]`)
			.notNull()
	},
	(t) => [
		index('ix_community_created_by').on(t.createdBy),
		index('ix_community_scopes').using('gin', t.scopes),
		index('ix_community_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		...scopedPolicies(t, { deletable: true })
	]
);

export const profile = pgTable(
	'profile',
	{
		embedding: halfvec('embedding', { dim: 1024 }),
		...timestamps,
		...scoped,
		...id,
		subjectId: uuid('subject_id')
			.notNull()
			.references(() => entityContent.id, { onDelete: 'cascade' }),
		summary: text('summary').notNull()
	},
	(t) => [
		index('ix_profile_subject_id').on(t.subjectId),
		index('ix_profile_created_by').on(t.createdBy),
		index('ix_profile_scopes').using('gin', t.scopes),
		index('ix_profile_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		unique('uq_profile_scope_subject').on(t.scopes, t.subjectId),
		...scopedPolicies(t, { mutable: true })
	]
);

export const sessionItem = pgTable(
	'session_item',
	{
		embedding: halfvec('embedding', { dim: 1024 }),
		...timestamps,
		...scoped,
		...id,
		kind: varchar('kind').notNull(),
		text: text('text').notNull(),
		provenance: jsonb('provenance').default(sql`'{}'::jsonb`).notNull(),
		promotedAt: timestamp('promoted_at', { withTimezone: true })
	},
	(t) => [
		index('ix_session_item_created_by').on(t.createdBy),
		index('ix_session_item_promoted_at').on(t.promotedAt),
		index('ix_session_item_scopes').using('gin', t.scopes),
		index('ix_session_item_embedding').using('vchordrq', t.embedding.op('halfvec_cosine_ops')),
		...scopedPolicies(t, { mutable: true })
	]
);

export const watermark = pgTable(
	'watermark',
	{
		...timestamps,
		...scoped,
		...id,
		kind: watermarkKind('kind').notNull(),
		ref: text('ref').default('global').notNull(),
		counter: bigint('counter', { mode: 'number' }).default(0).notNull(),
		payload: jsonb('payload').default(sql`'{}'::jsonb`).notNull()
	},
	(t) => [
		index('ix_watermark_created_by').on(t.createdBy),
		index('ix_watermark_scopes').using('gin', t.scopes),
		unique('uq_watermark_scope_kind_ref').on(t.scopes, t.kind, t.ref),
		...scopedPolicies(t, { mutable: true })
	]
);

export const entityKind = pgTable('entity_kind', {
	...timestamps,
	name: text('name').primaryKey(),
	description: text('description').notNull(),
	domain: text('domain').notNull(),
	structural: boolean('structural').default(false).notNull()
});

export const relationKind = pgTable('relation_kind', {
	...timestamps,
	name: text('name').primaryKey(),
	description: text('description').notNull(),
	domain: text('domain').notNull(),
	structural: boolean('structural').default(false).notNull()
});

// The live-claim join recall and the graph passes read, invoker-secured so RLS still
// applies to the caller, barrier-secured so the planner cannot leak rows past the policies.
export const liveFact = pgView('live_fact')
	.with({ securityInvoker: true, securityBarrier: true })
	.as((qb) =>
		qb
			.select({
				id: factClaim.id,
				contentId: factClaim.contentId,
				createdBy: factClaim.createdBy,
				scopes: factClaim.scopes,
				valid: factClaim.valid,
				recorded: factClaim.recorded,
				lastAccessed: factClaim.lastAccessed,
				accessCount: factClaim.accessCount,
				attributes: factClaim.attributes,
				perspectiveKey: factClaim.perspectiveKey,
				sourceChunkId: factClaim.sourceChunkId,
				promotedFrom: factClaim.promotedFrom,
				subjectId: factContent.subjectId,
				objectId: factContent.objectId,
				predicate: factContent.predicate,
				statement: factContent.statement,
				embedding: factContent.embedding
			})
			.from(factClaim)
			.innerJoin(factContent, sql`${factContent.id} = ${factClaim.contentId}`)
			.where(
				sql`upper_inf(${factClaim.recorded}) AND (${factClaim.valid} IS NULL OR ${factClaim.valid} @> now())`
			)
	);
