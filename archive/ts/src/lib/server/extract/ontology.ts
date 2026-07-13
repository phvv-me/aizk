// The live ontology's extraction-facing surface, ported from the Python extract/ontology
// package: structural constants, and a snapshot built once from entity_kind/relation_kind
// carrying the narrowed wire schema for the combined LLM call, the system prompt rendered
// from the template, and the entity-description vectors the suggested-type resolver maps
// against. The snapshot caches at module level and refreshes on demand, like the Python
// module-level cache every ontology write invalidates.
import { sql } from 'drizzle-orm';

import type { Tx } from '../db';
import { embedVectors } from '../serving';
import { settings } from '../settings';

// The handful of entity types referenced by name in code rather than only through the live
// catalog, the extract/ontology/constants.py enums flattened to their snake_case values.
export const RAPTOR_SUMMARY = 'raptor_summary';
export const OBSERVATION = 'observation';
export const PROJECT = 'project';
export const AREA = 'area';
export const CONCEPT = 'concept';

export const OBSERVES = 'observes';
export const RELATED_TO = 'related_to';
export const DEPENDS_ON = 'depends_on';
export const PART_OF = 'part_of';
export const CITES = 'cites';
export const SUPERSEDES = 'supersedes';

// One json_schema payload for a schema-constrained chat call.
export interface WireSchema {
	name: string;
	schema: object;
}

// The live ontology's whole extraction-facing surface.
export interface OntologySnapshot {
	entityNames: string[];
	relationNames: string[];
	entityDescriptions: Record<string, string>;
	entityDescriptionVectors: Record<string, number[]>;
	llmExtraction: WireSchema;
	prompt: string;
}

// The combined extraction call's wire schema, its enum fields narrowed to exactly the given
// names, mirroring the pydantic WireEntity/WireFact/WireExtraction models.
const wireSchema = (entityNames: string[], relationNames: string[]): WireSchema => ({
	name: 'LLMExtraction',
	schema: {
		type: 'object',
		properties: {
			e: {
				type: 'array',
				items: {
					type: 'object',
					properties: {
						n: {
							type: 'string',
							description: 'plain human-readable noun phrase, never a slug or identifier'
						},
						t: { type: 'string', enum: entityNames },
						suggested_type: {
							type: ['string', 'null'],
							description: 'a more specific type name when t had to fall back to Concept'
						}
					},
					required: ['n', 't', 'suggested_type'],
					additionalProperties: false
				}
			},
			f: {
				type: 'array',
				items: {
					type: 'object',
					properties: {
						s: { type: 'string' },
						p: { type: 'string', enum: relationNames },
						o: { type: 'string' },
						statement: {
							type: 'string',
							description: 'self-contained sentence that stands without source text'
						},
						quote: {
							type: ['string', 'null'],
							description:
								'shortest verbatim excerpt copied exactly from the text supporting this fact'
						},
						date: { type: ['string', 'null'] },
						k: {
							type: 'string',
							enum: [
								'world',
								'experience',
								'observation',
								'opinion',
								'preference',
								'procedure',
								'negative_result'
							]
						}
					},
					required: ['s', 'p', 'o', 'statement', 'quote', 'date', 'k'],
					additionalProperties: false
				}
			}
		},
		required: ['e', 'f'],
		additionalProperties: false
	}
});

// Read the current catalog and build a fresh snapshot from it.
export const buildSnapshot = async (tx: Tx): Promise<OntologySnapshot> => {
	const names = async (table: 'entity_kind' | 'relation_kind'): Promise<string[]> => {
		const rows = (await tx.execute(
			sql`select name from ${sql.raw(table)} where structural = false order by name`
		)) as unknown as { name: string }[];
		return rows.map((row) => row.name);
	};
	const entityNames = await names('entity_kind');
	const relationNames = await names('relation_kind');
	const described = (await tx.execute(
		sql`select name, description from entity_kind where structural = false order by name`
	)) as unknown as { name: string; description: string }[];
	const vectors = described.length
		? await embedVectors(
				described.map((row) => row.description),
				'document'
			)
		: [];
	const prompt = settings.ontologyPromptTemplate
		.replaceAll('{entity_count}', `${entityNames.length}`)
		.replaceAll('{entity_types}', entityNames.join(', '))
		.replaceAll('{relation_count}', `${relationNames.length}`)
		.replaceAll('{relation_types}', relationNames.join(', '));
	return {
		entityNames,
		relationNames,
		entityDescriptions: Object.fromEntries(described.map((row) => [row.name, row.description])),
		entityDescriptionVectors: Object.fromEntries(
			described.map((row, index) => [row.name, vectors[index]])
		),
		llmExtraction: wireSchema(entityNames, relationNames),
		prompt
	};
};

let snapshot: OntologySnapshot | null = null;

// Rebuild and cache the ontology snapshot, the call every ontology write makes afterward.
export const refresh = async (tx: Tx): Promise<OntologySnapshot> => {
	snapshot = await buildSnapshot(tx);
	return snapshot;
};

// The cached snapshot, raising if refresh has never run.
export const current = (): OntologySnapshot => {
	if (snapshot === null) throw new Error('ontology cache never refreshed, call refresh() first');
	return snapshot;
};

// Return the current snapshot, loading it once for a fresh process.
export const ensureCurrent = async (tx: Tx): Promise<OntologySnapshot> =>
	snapshot ?? refresh(tx);

// Entity kind names the GLiNER2 gate scores a chunk against, Concept excluded.
export const gateLabels = (): string[] =>
	current().entityNames.filter((name) => name !== CONCEPT);
