// The extraction pipeline's data shapes, ported from the Python extract/models package and
// provenance.py: extracted entities, dated facts, consolidation verdicts, the epistemic
// kinds with their speaker-bound perspective partitioning, and the portable CaptureContext
// carried in chunk provenance. observed_at stays the raw JSON string end to end so the
// stored attribute round-trips byte-identically with the Python pydantic serialization.

export type EpistemicKind =
	| 'world'
	| 'experience'
	| 'observation'
	| 'opinion'
	| 'preference'
	| 'procedure'
	| 'negative_result';

const SPEAKER_BOUND: ReadonlySet<EpistemicKind> = new Set([
	'experience',
	'observation',
	'opinion',
	'preference'
]);

// Whether two speakers may hold distinct versions of this kind.
export const speakerBound = (kind: EpistemicKind): boolean => SPEAKER_BOUND.has(kind);

// The consolidation partition for one kind and creator.
export const perspectiveKey = (kind: EpistemicKind, createdBy: string): string =>
	speakerBound(kind) ? `speaker:${createdBy}` : 'world';

// An entity proposed by the extractor, before resolution to a stored node.
export interface ExtractedEntity {
	name: string;
	type: string;
	suggestedType: string | null;
}

// A structural fact from the combined extraction, already dated, the candidate the
// consolidation consumes.
export interface TimedFact {
	subject: string;
	predicate: string;
	object: string;
	statement: string;
	quote: string | null;
	validFrom: Date | null;
	validTo: Date | null;
	kind: EpistemicKind;
}

// The structural graph slice from one text span, the output of the combined single call.
export interface Extraction {
	entities: ExtractedEntity[];
	facts: TimedFact[];
}

// The decision on how a new fact relates to the existing latest facts.
export interface ConsolidationVerdict {
	action: 'ADD' | 'UPDATE' | 'NOOP';
	supersedes: string | null;
}

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

// Portable speaker and conversation context captured with one source span.
export interface CaptureContext {
	speakerLabel: string | null;
	speakerRole: string | null;
	channel: string | null;
	replyTo: string | null;
	phase: string | null;
	topic: string | null;
	observedAt: string | null;
}

export const emptyCapture = (): CaptureContext => ({
	speakerLabel: null,
	speakerRole: null,
	channel: null,
	replyTo: null,
	phase: null,
	topic: null,
	observedAt: null
});

// Validate a chunk's stored provenance back into a capture, the CaptureContext.model_validate twin.
export const captureFrom = (provenance: Record<string, unknown>): CaptureContext => {
	const field = (name: string): string | null => {
		const value = provenance[name];
		return typeof value === 'string' ? value : null;
	};
	return {
		speakerLabel: field('speaker_label'),
		speakerRole: field('speaker_role'),
		channel: field('channel'),
		replyTo: field('reply_to'),
		phase: field('phase'),
		topic: field('topic'),
		observedAt: field('observed_at')
	};
};

// Render non-null fields for JSONB storage.
export const captureRecord = (capture: CaptureContext): Record<string, JsonValue> => {
	const fields: [string, string | null][] = [
		['speaker_label', capture.speakerLabel],
		['speaker_role', capture.speakerRole],
		['channel', capture.channel],
		['reply_to', capture.replyTo],
		['phase', capture.phase],
		['topic', capture.topic],
		['observed_at', capture.observedAt]
	];
	return Object.fromEntries(fields.filter(([, value]) => value !== null));
};

// Prefix text with speaker and conversation terms for embedding and lexical search.
export const searchText = (capture: CaptureContext, text: string): string => {
	const context = [
		capture.speakerLabel ? `speaker ${capture.speakerLabel}` : null,
		capture.speakerRole ? `role ${capture.speakerRole}` : null,
		capture.channel ? `channel ${capture.channel}` : null,
		capture.phase ? `phase ${capture.phase}` : null,
		capture.topic ? `topic ${capture.topic}` : null
	].filter((value): value is string => value !== null);
	return context.length ? [...context, text].join('\n') : text;
};

// Build fact attributes that preserve epistemic and speaker provenance.
export const claimAttributes = (
	capture: CaptureContext,
	kind: EpistemicKind,
	createdBy: string
): Record<string, JsonValue> => ({
	...captureRecord(capture),
	epistemic_kind: kind,
	perspective_key: perspectiveKey(kind, createdBy)
});

// The capture's observation instant as a date, for the document-fallback dating.
export const observedAtDate = (capture: CaptureContext): Date | null =>
	capture.observedAt === null ? null : new Date(capture.observedAt);
