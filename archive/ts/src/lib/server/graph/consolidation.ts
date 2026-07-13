// The deterministic consolidation cascade ported from graph/consolidation.py: rank a new
// fact's nearest existing claims by cosine distance and decide ADD/UPDATE/NOOP by rule
// alone when the similarity is unambiguous, leaving null for the genuinely borderline
// candidates one batched LLM call settles.
import type { ConsolidationVerdict } from '../extract/models';
import { settings } from '../settings';

const borderlineDistance = 1.0 - settings.consolidationBorderlineFloor;
const automaticDistance = 1.0 - settings.consolidationAutoMergeThreshold;

// The narrow current fact projection needed to consolidate one candidate.
export interface FactMatch {
	id: string;
	predicate: string;
	objectId: string | null;
	statement: string;
	distance: number;
}

// Cosine similarity between two equal-length dense vectors, no server round trip.
export const cosineSimilarity = (a: number[], b: number[]): number => {
	let dot = 0;
	let normA = 0;
	let normB = 0;
	for (let index = 0; index < a.length; index += 1) {
		dot += a[index] * b[index];
		normA += a[index] * a[index];
		normB += b[index] * b[index];
	}
	const magnitude = Math.sqrt(normA) * Math.sqrt(normB);
	return magnitude ? dot / magnitude : 0.0;
};

// Settle a would-be ADD by looking past the top match for a same-predicate claim.
const samePredicateVerdict = (
	predicate: string,
	objectId: string | null,
	matches: readonly FactMatch[]
): ConsolidationVerdict | null => {
	const match = matches.find((candidate) => candidate.predicate === predicate);
	if (match === undefined) return { action: 'ADD', supersedes: null };
	if (match.distance > borderlineDistance) return { action: 'ADD', supersedes: null };
	if (match.distance > automaticDistance) return null;
	if (match.objectId === objectId) return { action: 'NOOP', supersedes: null };
	return { action: 'UPDATE', supersedes: match.id };
};

// Decide a candidate fact's ADD/UPDATE/NOOP verdict from cosine distance alone, when possible.
export const decideByRule = (
	predicate: string,
	objectId: string | null,
	matches: readonly FactMatch[]
): ConsolidationVerdict | null => {
	if (!matches.length) return { action: 'ADD', supersedes: null };
	const best = matches[0];
	if (best.distance > borderlineDistance) return { action: 'ADD', supersedes: null };
	if (best.distance > automaticDistance) return null;
	if (best.predicate === predicate && best.objectId === objectId)
		return { action: 'NOOP', supersedes: null };
	if (best.predicate === predicate) return { action: 'UPDATE', supersedes: best.id };
	return samePredicateVerdict(predicate, objectId, matches);
};
