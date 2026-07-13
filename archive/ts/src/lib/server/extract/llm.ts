// The LLM seams of the extraction pipeline, ported from extract/llm/ and
// extract/strategies.py: the ontology-layered system prompt, the combined wire-schema
// extraction call, the strategy dispatch, the named provider overlay, and the one batched
// consolidation call that decides every genuinely borderline fact.
import { env } from 'node:process';

import type { FactMatch } from '../graph/consolidation';
import { structured } from '../serving';
import { settings } from '../settings';
import { resolveValidFrom } from './dating';
import * as ontology from './ontology';
import type {
	ConsolidationVerdict,
	EpistemicKind,
	Extraction,
	TimedFact
} from './models';

interface WireEntity {
	n: string;
	t: string;
	suggested_type: string | null;
}

interface WireFact {
	s: string;
	p: string;
	o: string;
	statement: string;
	quote: string | null;
	date: string | null;
	k: EpistemicKind;
}

interface WireExtraction {
	e: WireEntity[];
	f: WireFact[];
}

// A named OpenAI-compatible chat endpoint; explicit presets override only settings that
// still hold their defaults, the extract/llm/providers.py overlay.
interface Provider {
	url: string;
	model: string;
	apiKey: string;
}

const PROVIDERS: Record<string, Provider> = {
	ollama: { url: 'http://localhost:11434/v1', model: 'qwen2.5:7b', apiKey: '' },
	cerebras: { url: 'https://api.cerebras.ai/v1', model: 'llama-3.3-70b', apiKey: '' },
	deepseek: { url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', apiKey: '' },
	openai: { url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', apiKey: '' }
};

// Whether a settings field still holds its default, read from the environment because the
// TS settings object never records which values came from AIZK_ variables.
const atDefault = (name: string): boolean => !env[`AIZK_${name}`];

// Overlay a named provider's endpoint onto the settings, leaving explicit overrides alone.
export const providerSettings = (): { url: string; model: string; apiKey: string } => {
	const provider = PROVIDERS[settings.llmProvider];
	if (provider === undefined)
		return { url: settings.llmUrl, model: settings.llmModel, apiKey: settings.llmApiKey };
	return {
		url: atDefault('LLM_URL') ? provider.url : settings.llmUrl,
		model: atDefault('LLM_MODEL') ? provider.model : settings.llmModel,
		apiKey: provider.apiKey && atDefault('LLM_API_KEY') ? provider.apiKey : settings.llmApiKey
	};
};

// The ontology default strategy's system turn, the live ontology rules layered on the
// few-shot guidance that keeps entity names and facts well formed.
export const extractionSystem = (): string =>
	`${ontology.current().prompt}\n${settings.extractSystemPrompt}`;

// Run the combined wire-schema extraction call under a given system prompt.
export const extractWithSystem = async (system: string, text: string): Promise<Extraction> => {
	const provider = providerSettings();
	const wire = await structured<WireExtraction>(system, text, ontology.current().llmExtraction, {
		url: provider.url,
		model: provider.model,
		apiKey: provider.apiKey
	});
	return {
		entities: wire.e.map((entity) => ({
			name: entity.n,
			type: entity.t,
			suggestedType: entity.suggested_type ?? null
		})),
		facts: wire.f.map((fact) => ({
			subject: fact.s,
			predicate: fact.p,
			object: fact.o ?? '',
			statement: fact.statement,
			quote: fact.quote ?? null,
			validFrom: resolveValidFrom(fact.date ?? null, fact.statement),
			validTo: null,
			kind: fact.k ?? 'world'
		}))
	};
};

// Extract entities, facts, and each fact's own date under the ontology default strategy.
export const combinedExtract = (text: string): Promise<Extraction> =>
	extractWithSystem(extractionSystem(), text);

// The graph detail level applied to one extraction call, extract/strategies.py's dispatch.
const strategySystem = (strategy: string): string => {
	const focus =
		strategy === 'summary'
			? settings.extractSummaryPrompt
			: strategy === 'preferences'
				? settings.extractPreferencesPrompt
				: strategy === 'custom'
					? settings.extractCustomPrompt
					: '';
	return focus ? `${ontology.current().prompt}\n${focus}` : extractionSystem();
};

// Extract a graph slice with the configured typed strategy.
export const extractGraph = (text: string): Promise<Extraction> => {
	const strategy = settings.extractStrategy;
	if (strategy === 'ontology' || (strategy === 'custom' && !settings.extractCustomPrompt))
		return combinedExtract(text);
	return extractWithSystem(strategySystem(strategy), text);
};

// Render one candidate's new fact and its existing similar claims as a numbered prompt block.
export const consolidationBlock = (
	index: number,
	fact: TimedFact,
	existing: FactMatch[]
): string => {
	const catalog =
		existing.map((claim) => `  id=${claim.id} statement=${claim.statement}`).join('\n') ||
		'  (none)';
	return `${index}. New fact: ${fact.statement}\nExisting facts.\n${catalog}`;
};

interface BatchConsolidationVerdict {
	verdicts: { action: 'ADD' | 'UPDATE' | 'NOOP'; supersedes: string | null }[];
}

const VERDICT_SCHEMA = {
	name: 'BatchConsolidationVerdict',
	schema: {
		type: 'object',
		properties: {
			verdicts: {
				type: 'array',
				items: {
					type: 'object',
					properties: {
						action: { type: 'string', enum: ['ADD', 'UPDATE', 'NOOP'] },
						supersedes: { type: ['string', 'null'], format: 'uuid' }
					},
					required: ['action', 'supersedes'],
					additionalProperties: false
				}
			}
		},
		required: ['verdicts'],
		additionalProperties: false
	}
};

// Resolve one candidate's verdict, dropping a supersedes id the batch call hallucinated.
export const resolveVerdict = (
	index: number,
	existing: FactMatch[],
	resolution: BatchConsolidationVerdict
): ConsolidationVerdict => {
	const known = new Set(existing.map((claim) => claim.id));
	const verdict = resolution.verdicts[index] ?? { action: 'ADD' as const, supersedes: null };
	const supersedes =
		verdict.action === 'UPDATE' && verdict.supersedes !== null && known.has(verdict.supersedes)
			? verdict.supersedes
			: null;
	return { action: verdict.action, supersedes };
};

// Decide ADD/UPDATE/NOOP for every borderline fact in one call.
export const decideConsolidationsBatch = async (
	candidates: [TimedFact, FactMatch[]][]
): Promise<ConsolidationVerdict[]> => {
	if (!candidates.length) return [];
	const user = candidates
		.map(([fact, existing], index) => consolidationBlock(index, fact, existing))
		.join('\n\n');
	const provider = providerSettings();
	const resolution = await structured<BatchConsolidationVerdict>(
		settings.consolidationPrompt,
		user,
		VERDICT_SCHEMA,
		{ url: provider.url, model: provider.model, apiKey: provider.apiKey }
	);
	return candidates.map(([, existing], index) => resolveVerdict(index, existing, resolution));
};
