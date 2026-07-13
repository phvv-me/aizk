// HTTP clients for the model-serving seams, mirroring the Python serving/ package: the
// OpenAI-compatible embedder, the GLiNER2 sidecar's classification and extraction heads,
// and the cross-encoder reranker wrapped in its prompt scaffold.
import { chunk as batched, uniq } from 'es-toolkit';

import { settings } from './settings';

const post = async (
	url: string,
	body: object,
	headers: Record<string, string> = {},
	timeoutSeconds = 30
): Promise<Record<string, unknown>> => {
	const response = await fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', ...headers },
		body: JSON.stringify(body),
		signal: AbortSignal.timeout(timeoutSeconds * 1000)
	});
	if (!response.ok) throw new Error(`${url} failed: ${response.status}`);
	return (await response.json()) as Record<string, unknown>;
};

export type EmbedMode = 'query' | 'document';

// Raw embedding vectors aligned to the input order, requested in embedder-sized batches so
// a corpus shares efficient requests; the twin of the Python serving/embed call.
export const embedVectors = async (
	texts: string[],
	mode: EmbedMode = 'document'
): Promise<number[][]> => {
	const inputs =
		mode === 'query'
			? texts.map((text) => `Instruct: ${settings.embedInstructionQuery}\nQuery: ${text}`)
			: texts;
	const vectors: number[][] = [];
	for (const batch of batched(inputs, settings.embedBatchSize)) {
		const payload = (await post(
			`${settings.embedUrl}/embeddings`,
			{
				model: settings.embedModel,
				input: batch,
				dimensions: settings.embedDim,
				encoding_format: 'float'
			},
			{ Authorization: 'Bearer local' }
		)) as unknown as { data: { index: number; embedding: number[] }[] };
		vectors.push(...payload.data.sort((a, b) => a.index - b.index).map((row) => row.embedding));
	}
	return vectors;
};

// Render one raw vector as the pgvector literal a halfvec parameter binds as.
export const vectorLiteral = (vector: number[]): string => `[${vector.join(',')}]`;

// Vectors as pgvector literals ready to bind, aligned to the input order.
export const embed = async (texts: string[], mode: EmbedMode = 'document'): Promise<string[]> =>
	(await embedVectors(texts, mode)).map(vectorLiteral);

const gate = async (route: string, body: object): Promise<Record<string, unknown>> =>
	post(`${settings.glinerGateUrl}${route}`, body, {}, settings.glinerGateTimeout);

export const classify = async (text: string, task: string, labels: string[]): Promise<string> => {
	const result = await gate('/classify', { text, tasks: { [task]: labels } });
	return String(result[task]);
};

// Whether text carries an ontology type worth extracting, the twin of the Python gate's
// relevant(); the caller passes the gate labels so this layer never reads the ontology.
export const relevant = async (text: string, labels: string[]): Promise<boolean> => {
	const result = await gate('/classify', {
		text,
		tasks: {
			present: {
				labels,
				multi_label: true,
				cls_threshold: settings.glinerGateThreshold
			}
		}
	});
	const value = result.present;
	if (!Array.isArray(value)) throw new Error('GLiNER2 returned invalid labels for present');
	const floor = new Set(
		settings.glinerGateFloor
			.split(',')
			.map((label) => label.trim())
			.filter(Boolean)
	);
	return value.some((label) => typeof label === 'string' && !floor.has(label));
};

// Chunk text into spans through the chonkie sidecar at AIZK_CHUNK_URL, the seam replacing
// Python's in-process chunker. AIZK_CHUNKER=naive is a verification-only override that
// splits on blank lines instead of calling the sidecar.
export const chunkSpans = async (text: string, kind: string): Promise<string[]> => {
	if (settings.chunker === 'naive')
		return text
			.split(/\n\s*\n/)
			.map((span) => span.trim())
			.filter(Boolean);
	const payload = (await post(
		`${settings.chunkUrl}/chunk`,
		{ text, kind, chunk_size: settings.chunkSize },
		{},
		settings.glinerGateTimeout
	)) as unknown as { spans: string[] };
	return payload.spans.map((span) => span.trim()).filter(Boolean);
};

export const mentions = async (text: string, entityTypes: string[]): Promise<string[]> => {
	const result = await gate('/extract', { text, entity_types: entityTypes, threshold: 0.7 });
	const groups = (result.entities ?? {}) as Record<string, string[]>;
	const spans = Object.values(groups).flat();
	return uniq(spans.map((span) => span.trim().toLowerCase()).filter(Boolean)).sort();
};

// Run one schema-constrained chat turn against the OpenAI-compatible LLM endpoint and
// return the parsed JSON, the twin of the Python extract/llm structured() call.
export const structured = async <T>(
	system: string,
	user: string,
	schema: { name: string; schema: object },
	options: {
		temperature?: number;
		timeout?: number;
		maxTokens?: number;
		url?: string;
		model?: string;
		apiKey?: string;
	} = {}
): Promise<T> => {
	const apiKey = options.apiKey ?? settings.llmApiKey;
	const headers: Record<string, string> = apiKey
		? { Authorization: `Bearer ${apiKey}` }
		: { Authorization: 'Bearer local' };
	const payload = (await post(
		`${options.url ?? settings.llmUrl}/chat/completions`,
		{
			model: options.model ?? settings.llmModel,
			temperature: options.temperature ?? settings.extractTemperature,
			max_tokens: options.maxTokens ?? settings.extractMaxTokens,
			response_format: {
				type: 'json_schema',
				json_schema: { name: schema.name, strict: true, schema: schema.schema }
			},
			messages: [
				{ role: 'system', content: system },
				{ role: 'user', content: user }
			]
		},
		headers,
		options.timeout ?? settings.extractTimeout
	)) as unknown as { choices: { message: { content: string } }[] };
	return JSON.parse(payload.choices[0].message.content) as T;
};

export const rerankEnabled = (): boolean => Boolean(settings.rerankUrl);

// Score texts against a query through the cross-encoder, aligned to the input order. The
// scaffold templates are load-bearing: the Qwen3 checkpoint calibrates its yes/no scores
// to this exact prompt shape, and empty templates send the raw texts.
export const rerank = async (query: string, texts: string[]): Promise<number[]> => {
	if (!texts.length) return [];
	const wrappedQuery = settings.rerankQueryTemplate
		? settings.rerankQueryTemplate
				.replaceAll('{instruction}', settings.rerankInstruction)
				.replaceAll('{query}', query)
		: query;
	const documents = texts.map((text) =>
		settings.rerankDocumentTemplate
			? settings.rerankDocumentTemplate.replaceAll('{document}', text)
			: text
	);
	const headers: Record<string, string> = settings.rerankApiKey
		? { Authorization: `Bearer ${settings.rerankApiKey}` }
		: {};
	const payload = (await post(
		`${settings.rerankUrl.replace(/\/$/, '')}/rerank`,
		{ model: settings.rerankModel, query: wrappedQuery, documents },
		headers,
		settings.rerankRequestTimeout
	)) as unknown as { results: { index: number; relevance_score: number }[] };
	if (payload.results.length !== texts.length)
		throw new Error(`reranker returned ${payload.results.length} scores for ${texts.length} texts`);
	const scores = new Array<number>(texts.length).fill(0);
	for (const result of payload.results) scores[result.index] = result.relevance_score;
	return scores;
};
