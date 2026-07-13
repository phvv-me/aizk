// One graph-write round bound to the exact scope set every write in it shares, ported from
// graph/writer.py. Entity resolution is one VALUES-CTE read with a lateral nearest-entity
// probe plus bulk content and claim writes; fact planning ranks the narrow top matches in
// PostgreSQL through a lateral per candidate; lock_plans serializes each scope, subject and
// perspective slot through hashtextextended advisory locks; and apply_plans writes decided
// plans with the supersede valid-range bookkeeping and live-index on-conflict claim
// inserts. Content inserts land as ON CONFLICT DO NOTHING, the set-level twin of the
// Python mint_all savepoint dance with the same end state.
import { sql, type SQL } from 'drizzle-orm';
import { v7 as uuid7 } from 'uuid';

import { quoteInterval } from '../extract/grounding';
import {
	claimAttributes,
	perspectiveKey,
	type CaptureContext,
	type ConsolidationVerdict,
	type JsonValue,
	type TimedFact
} from '../extract/models';
import { vectorLiteral } from '../serving';
import { settings } from '../settings';
import type { Tx } from '../db';
import { entityClaim, entityContent, factClaim, factContent } from '../db/schema';
import { decideByRule, type FactMatch } from './consolidation';
import { entityId, factId } from './ids';
import { normalizeName } from './naming';
import { uuidArray } from './system';

const n = (value: number): SQL => sql.raw(value.toFixed(0));

// A '[)' tstzrange literal with optional bounds, the asyncpg Range default rendering.
const rangeLiteral = (lower: Date | null, upper: Date | null): string =>
	`[${lower === null ? '' : `"${lower.toISOString()}"`},${upper === null ? '' : `"${upper.toISOString()}"`})`;

// An extracted entity with its resolved type and precomputed name embedding.
export interface PreparedEntity {
	name: string;
	type: string;
	vector: number[];
}

// A new scoped fact whose referenced entities have already resolved.
export interface FactCandidate {
	fact: TimedFact;
	subjectId: string;
	objectId: string | null;
	identity: string;
}

// A fact candidate with its vector, ranked pool, and rule verdict.
export interface FactPlan {
	candidate: FactCandidate;
	vector: number[];
	matches: FactMatch[];
	verdict: ConsolidationVerdict | null;
}

// Fill each null, genuinely-ambiguous slot with the batched LLM's own verdict, in order.
export const mergedVerdicts = (
	verdicts: (ConsolidationVerdict | null)[],
	resolved: ConsolidationVerdict[]
): (ConsolidationVerdict | null)[] => {
	let next = 0;
	return verdicts.map((verdict) => verdict ?? resolved[next++]);
};

export class GraphWriter {
	readonly createdBy: string;
	readonly scopes: string[];
	readonly capture: CaptureContext;
	readonly sourceText: string;

	constructor(createdBy: string, scopes: string[], capture: CaptureContext, sourceText = '') {
		this.createdBy = createdBy;
		this.scopes = [...new Set(scopes)].sort();
		this.capture = capture;
		this.sourceText = sourceText;
	}

	// Char offsets of the fact's supporting quote inside the source chunk, when it aligns.
	grounding(fact: TimedFact): Record<string, JsonValue> {
		const interval = quoteInterval(fact.quote, this.sourceText);
		if (interval === null) return {};
		return { quote_start: interval[0], quote_end: interval[1] };
	}

	// Resolve a chunk's entities with one claim read and bulk content and claim writes.
	async resolveAll(tx: Tx, entities: PreparedEntity[]): Promise<Map<string, string>> {
		const usable: [PreparedEntity, string][] = [];
		for (const entity of entities) {
			if (normalizeName(entity.name)) usable.push([entity, entityId(entity.name, entity.type)]);
			else console.warn(`entity name ${JSON.stringify(entity.name)} is a path or link, dropping`);
		}
		if (!usable.length) return new Map();
		const inputs = usable.map(
			([entity, node], ordinal) =>
				sql`(${n(ordinal)}::integer, ${node}::uuid, ${entity.type}::text,
					${vectorLiteral(entity.vector)}::halfvec(${n(settings.embedDim)}))`
		);
		const rows = (await tx.execute(sql`
			with entity_input (ordinal, id, type, embedding) as (values ${sql.join(inputs, sql`, `)})
			select ei.ordinal as ordinal,
				coalesce(cl.content_id, nearest.id, ei.id) as resolved,
				(cl.content_id is null and nearest.id is null) as is_new
			from entity_input ei
			left join entity_claim cl
				on cl.content_id = ei.id and cl.scopes = ${uuidArray(this.scopes)}::uuid[]
			left join lateral (
				select ec.id from entity_content ec
				where ec.type = ei.type
					and (ec.embedding <=> ei.embedding) <= ${1.0 - settings.entityResolutionThreshold}
				order by ec.embedding <=> ei.embedding
				limit 1
			) nearest on cl.content_id is null
			order by ei.ordinal
		`)) as unknown as { ordinal: number; resolved: string; is_new: boolean }[];
		const resolved = new Map<string, string>();
		const newContents = new Map<string, PreparedEntity>();
		for (const row of rows) {
			const [entity, node] = usable[row.ordinal];
			resolved.set(entity.name, row.resolved);
			if (row.is_new) newContents.set(node, entity);
		}
		if (newContents.size)
			await tx
				.insert(entityContent)
				.values(
					[...newContents.entries()].map(([node, entity]) => ({
						id: node,
						name: entity.name,
						type: entity.type,
						embedding: vectorLiteral(entity.vector)
					}))
				)
				.onConflictDoNothing();
		const claimIds = [...new Set(resolved.values())];
		await tx
			.insert(entityClaim)
			.values(
				claimIds.map((contentId) => ({
					id: uuid7(),
					contentId,
					createdBy: this.createdBy,
					scopes: this.scopes
				}))
			)
			.onConflictDoNothing({ target: [entityClaim.contentId, entityClaim.scopes] });
		return resolved;
	}

	// Build a candidate when its subject resolved to a stored entity.
	candidate(fact: TimedFact, resolved: Map<string, string>): FactCandidate | null {
		const subjectId = resolved.get(fact.subject);
		if (subjectId === undefined) {
			console.warn(`fact subject ${JSON.stringify(fact.subject)} has no resolved entity, skipping`);
			return null;
		}
		const objectId = fact.object ? (resolved.get(fact.object) ?? null) : null;
		return {
			fact,
			subjectId,
			objectId,
			identity: factId(fact.subject, fact.predicate, fact.object, fact.statement)
		};
	}

	// The facts not already claimed by this container and whose subject resolved to a real
	// entity, the consolidation cascade's first, free tier. Python's ORM read runs under the
	// live temporal gate, so only current claims block a candidate here.
	async newCandidates(
		tx: Tx,
		facts: TimedFact[],
		resolved: Map<string, string>
	): Promise<FactCandidate[]> {
		const candidates = facts
			.map((fact) => this.candidate(fact, resolved))
			.filter((candidate): candidate is FactCandidate => candidate !== null);
		if (!candidates.length) return [];
		const keys = candidates.map(
			(candidate) =>
				sql`(${candidate.identity}::uuid, ${perspectiveKey(candidate.fact.kind, this.createdBy)}::varchar)`
		);
		const rows = (await tx.execute(sql`
			select content_id, perspective_key from fact_claim
			where scopes = ${uuidArray(this.scopes)}::uuid[]
				and (content_id, perspective_key) in (values ${sql.join(keys, sql`, `)})
				and upper_inf(recorded) and (valid is null or valid @> now())
		`)) as unknown as { content_id: string; perspective_key: string }[];
		const claimed = new Set(rows.map((row) => `${row.content_id}|${row.perspective_key}`));
		return candidates.filter(
			(candidate) =>
				!claimed.has(
					`${candidate.identity}|${perspectiveKey(candidate.fact.kind, this.createdBy)}`
				)
		);
	}

	// Rank the narrow top fact matches in PostgreSQL and apply deterministic verdicts.
	async planFacts(tx: Tx, candidates: FactCandidate[], vectors: number[][]): Promise<FactPlan[]> {
		if (!candidates.length) return [];
		const inputs = candidates.map(
			(candidate, ordinal) =>
				sql`(${n(ordinal)}::integer, ${candidate.subjectId}::uuid,
					${perspectiveKey(candidate.fact.kind, this.createdBy)}::text,
					${vectorLiteral(vectors[ordinal])}::halfvec(${n(settings.embedDim)}))`
		);
		const rows = (await tx.execute(sql`
			with fact_input (ordinal, subject_id, perspective_key, embedding) as (
				values ${sql.join(inputs, sql`, `)}
			)
			select fi.ordinal as ordinal, ranked.id as id, ranked.predicate as predicate,
				ranked.object_id as object_id, ranked.statement as statement, ranked.distance as distance
			from fact_input fi
			left join lateral (
				select cl.id, fc.predicate, fc.object_id, fc.statement,
					(fc.embedding <=> fi.embedding) as distance
				from fact_content fc
				join fact_claim cl on cl.content_id = fc.id
				where fc.subject_id = fi.subject_id
					and cl.scopes = ${uuidArray(this.scopes)}::uuid[]
					and cl.perspective_key = fi.perspective_key
					and upper_inf(cl.recorded) and (cl.valid is null or cl.valid @> now())
				order by distance
				limit ${n(settings.similarFacts)}
			) ranked on true
			order by fi.ordinal, ranked.distance
		`)) as unknown as {
			ordinal: number;
			id: string | null;
			predicate: string | null;
			object_id: string | null;
			statement: string | null;
			distance: number | null;
		}[];
		const matches: FactMatch[][] = candidates.map(() => []);
		for (const row of rows) {
			if (row.id === null) continue;
			matches[row.ordinal].push({
				id: row.id,
				predicate: row.predicate as string,
				objectId: row.object_id,
				statement: row.statement as string,
				distance: Number(row.distance)
			});
		}
		return candidates.map((candidate, ordinal) => ({
			candidate,
			vector: vectors[ordinal],
			matches: matches[ordinal],
			verdict: decideByRule(candidate.fact.predicate, candidate.objectId, matches[ordinal])
		}));
	}

	// Return only plans whose similarity needs one batched LLM decision.
	borderline(plans: FactPlan[]): [TimedFact, FactMatch[]][] {
		return plans
			.filter((plan) => plan.verdict === null)
			.map((plan) => [plan.candidate.fact, plan.matches]);
	}

	// Serialize each scope, subject, and perspective slot for final revalidation.
	async lockPlans(tx: Tx, plans: FactPlan[]): Promise<void> {
		const slots = [
			...new Map(
				plans.map((plan) => {
					const key = perspectiveKey(plan.candidate.fact.kind, this.createdBy);
					return [`${plan.candidate.subjectId}|${key}`, [plan.candidate.subjectId, key]];
				})
			).values()
		].sort(([a, aKey], [b, bKey]) => (a < b ? -1 : a > b ? 1 : aKey < bKey ? -1 : 1));
		if (!slots.length) return;
		const inputs = slots.map(
			([subjectId, key]) => sql`(${subjectId}::uuid, ${key}::text)`
		);
		const joined = this.scopes.join(',');
		const key = (alias: SQL): SQL =>
			sql`hashtextextended(${alias}.subject_id::text || '|' || ${alias}.perspective_key || '|' || ${joined}, 0)`;
		await tx.execute(sql`
			with fact_lock (subject_id, perspective_key) as (values ${sql.join(inputs, sql`, `)})
			select pg_advisory_xact_lock(${key(sql.raw('fl'))})
			from fact_lock fl
			order by ${key(sql.raw('fl'))}
		`);
	}

	// Apply already-decided plans inside one short write transaction.
	async applyPlans(
		tx: Tx,
		plans: FactPlan[],
		resolved: ConsolidationVerdict[],
		sourceChunkId: string
	): Promise<void> {
		const verdicts = mergedVerdicts(
			plans.map((plan) => plan.verdict),
			resolved
		);
		const superseded = verdicts
			.filter(
				(verdict): verdict is ConsolidationVerdict =>
					verdict !== null && verdict.action === 'UPDATE' && verdict.supersedes !== null
			)
			.map((verdict) => verdict.supersedes as string);
		const retired = new Map<string, { validLower: Date | null }>();
		if (superseded.length) {
			const rows = (await tx.execute(sql`
				select id, lower(valid) as valid_lower from fact_claim
				where id in (select (value)::uuid from jsonb_array_elements_text(${JSON.stringify(superseded)}::jsonb))
			`)) as unknown as { id: string; valid_lower: Date | null }[];
			for (const row of rows)
				retired.set(row.id, { validLower: row.valid_lower === null ? null : new Date(row.valid_lower) });
		}
		const contents: (typeof factContent.$inferInsert)[] = [];
		const claims: (typeof factClaim.$inferInsert)[] = [];
		for (let index = 0; index < plans.length; index += 1) {
			const plan = plans[index];
			const verdict = verdicts[index];
			if (verdict === null) throw new Error('unresolved consolidation verdict');
			if (verdict.action === 'NOOP') continue;
			const { candidate } = plan;
			const { fact } = candidate;
			const now = new Date();
			let validTo = fact.validTo;
			if (verdict.action === 'UPDATE' && verdict.supersedes !== null) {
				const previous = retired.get(verdict.supersedes);
				if (previous === undefined) throw new Error(`superseded claim ${verdict.supersedes} not found`);
				const lower = previous.validLower;
				if (fact.validFrom !== null && lower !== null && fact.validFrom < lower) {
					if (validTo === null || lower < validTo) validTo = lower;
				} else {
					let closing = fact.validFrom ?? now;
					if (lower !== null && closing < lower) closing = lower;
					await tx.execute(sql`
						update fact_claim
						set valid = ${rangeLiteral(lower, closing)}::tstzrange,
							recorded = tstzrange(lower(recorded), ${now.toISOString()}::timestamptz)
						where id = ${verdict.supersedes}::uuid
					`);
				}
			}
			contents.push({
				id: candidate.identity,
				subjectId: candidate.subjectId,
				objectId: candidate.objectId,
				predicate: fact.predicate,
				statement: fact.statement,
				embedding: vectorLiteral(plan.vector)
			});
			claims.push({
				id: uuid7(),
				contentId: candidate.identity,
				createdBy: this.createdBy,
				scopes: this.scopes,
				valid:
					fact.validFrom !== null || validTo !== null
						? rangeLiteral(fact.validFrom, validTo)
						: null,
				sourceChunkId,
				attributes: {
					...claimAttributes(this.capture, fact.kind, this.createdBy),
					...this.grounding(fact)
				},
				perspectiveKey: perspectiveKey(fact.kind, this.createdBy)
			});
		}
		if (contents.length) await tx.insert(factContent).values(contents).onConflictDoNothing();
		if (claims.length)
			await tx
				.insert(factClaim)
				.values(claims)
				.onConflictDoNothing({
					target: [factClaim.contentId, factClaim.scopes, factClaim.perspectiveKey],
					where: sql`upper_inf(recorded)`
				});
	}
}
