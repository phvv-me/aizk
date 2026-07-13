// Entity dedup, the TS twin of graph/repair.py: group visible entity content by normalized
// name and type as the system identity (RLS scopes what a run may merge), then in one
// owner-role transaction repoint or drop every fact naming a duplicate, migrate the
// duplicates' claims onto the canonical rows, and delete the duplicate nodes. Where Python
// deletes and reinserts a corrected fact content row with the same id and claim history,
// this port updates the endpoints in place, which lands the identical end state.
import { sql } from 'drizzle-orm';

import { normalizeName } from './naming';
import { asSystem, bypassRls, scopeKey, uuidArray } from './system';

const RAPTOR_SUMMARY = 'raptor_summary';

// Resolve one subject or object id through the duplicate-to-canonical redirect map.
const redirectEntity = (
	redirect: Map<string, string | null>,
	entityId: string | null
): [string | null, boolean] => {
	if (entityId === null) return [null, false];
	if (!redirect.has(entityId)) return [entityId, false];
	const replacement = redirect.get(entityId)!;
	return [replacement, replacement === null];
};

// Group visible entity content by normalized name and type, return the canonical redirect
// map: duplicates point at their canonical id, path-like names at null (drop).
const findDuplicates = (rows: { id: string; name: string; type: string }[]): Map<string, string | null> => {
	const entities = [...rows].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
	const canonical = new Map<string, string>();
	const redirect = new Map<string, string | null>();
	for (const entity of entities) {
		const normalized = normalizeName(entity.name);
		const group = `${entity.type}\x1f${normalized}`;
		const keep = normalized ? canonical.get(group) : undefined;
		if (normalized && keep === undefined) {
			canonical.set(group, entity.id);
			continue;
		}
		redirect.set(entity.id, keep ?? null);
	}
	return redirect;
};

interface AffectedFact {
	id: string;
	subject_id: string;
	object_id: string | null;
}

// Merge entity content sharing a normalized name and type, repoint claims, return the
// count, the dedup_entities twin.
export const dedupEntities = async (scopes?: string[]): Promise<number> => {
	const key = scopeKey(scopes);
	const { redirect, affected } = await asSystem(key, async (tx) => {
		const entities = (await tx.execute(sql`
			select id, name, type from entity_content where type != ${RAPTOR_SUMMARY}
		`)) as unknown as { id: string; name: string; type: string }[];
		const redirect = findDuplicates(entities);
		if (!redirect.size) return { redirect, affected: [] as AffectedFact[] };
		const duplicates = uuidArray([...redirect.keys()]);
		const affected = (await tx.execute(sql`
			select id, subject_id, object_id from fact_content
			where subject_id = any(${duplicates}::uuid[]) or object_id = any(${duplicates}::uuid[])
		`)) as unknown as AffectedFact[];
		return { redirect, affected };
	});
	if (!redirect.size) return 0;
	const drops: string[] = [];
	const repoints: { id: string; subject_id: string; object_id: string | null }[] = [];
	for (const fact of affected) {
		const [subject, subjectDropped] = redirectEntity(redirect, fact.subject_id);
		const [object, objectDropped] = redirectEntity(redirect, fact.object_id);
		if (subjectDropped || objectDropped || subject === null) drops.push(fact.id);
		else repoints.push({ id: fact.id, subject_id: subject, object_id: object });
	}
	return bypassRls(async (tx) => {
		if (drops.length)
			await tx.execute(sql`delete from fact_content where id = any(${uuidArray(drops)}::uuid[])`);
		if (repoints.length)
			await tx.execute(sql`
				update fact_content fc set subject_id = d.subject_id, object_id = d.object_id
				from jsonb_to_recordset(${JSON.stringify(repoints)}::jsonb)
					as d(id uuid, subject_id uuid, object_id uuid)
				where fc.id = d.id
			`);
		let merged = 0;
		for (const [duplicate, canonical] of redirect) {
			if (canonical !== null) {
				// Drop the duplicate's claims that would collide with an existing canonical
				// claim in the same scope set, then repoint the survivors.
				await tx.execute(sql`
					delete from entity_claim ec using entity_claim canon
					where ec.content_id = ${duplicate}
						and canon.content_id = ${canonical} and canon.scopes = ec.scopes
				`);
				await tx.execute(sql`
					update entity_claim set content_id = ${canonical} where content_id = ${duplicate}
				`);
			}
			await tx.execute(sql`delete from entity_content where id = ${duplicate}`);
			merged += 1;
		}
		return merged;
	});
};
