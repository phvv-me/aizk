// The authority seams every graph maintenance pass shares, mirroring the Python passes'
// two session kinds: `asSystem` runs one app-role transaction as the service identity over
// an exact scope set (acting_as(User.system(key))), so RLS filters reads and writes to that
// set, while `bypassRls` runs one owner-role transaction (engine.bypass_rls()) for the
// structural work RLS would block, deleting shared content and rewriting embeddings.
import { actingAs, adminDb, type Tx, type User } from '../db';
import { settings } from '../settings';

export type { Tx } from '../db';

// The Python passes key every generation by frozenset(scopes or (system_user_id,)) and
// compare with sorted(key); uuid string sort equals Postgres uuid byte order.
export const scopeKey = (scopes?: string[]): string[] => {
	const key = [...new Set(scopes?.length ? scopes : [settings.systemUserId])];
	return key.sort();
};

// postgres-js serializes a JS array parameter as a record, so uuid sets bind as one
// Postgres array literal string cast back to uuid[] (the promote.ts pattern).
export const uuidArray = (key: string[]): string => `{${key.join(',')}}`;

const system = (key: string[]): User => ({
	id: settings.systemUserId,
	read: key,
	write: key,
	public: []
});

export const asSystem = <T>(key: string[], work: (tx: Tx) => Promise<T>): Promise<T> =>
	actingAs(system(key), work);

export const bypassRls = <T>(work: (tx: Tx) => Promise<T>): Promise<T> => adminDb.transaction(work);

// One schema-constrained LLM call a pass can take injected, the seam that lets the smoke
// script run every pass against a deterministic reporter while production uses structured().
export type Reporter<T> = (system: string, user: string) => Promise<T>;

// Stored halfvec columns and serving.embed() both carry vectors as pgvector literals.
export const toFloats = (vector: string): number[] => JSON.parse(vector) as number[];

export const cosine = (a: number[], b: number[]): number => {
	let dot = 0;
	let normA = 0;
	let normB = 0;
	for (let i = 0; i < a.length; i++) {
		dot += a[i] * b[i];
		normA += a[i] * a[i];
		normB += b[i] * b[i];
	}
	const magnitude = Math.sqrt(normA) * Math.sqrt(normB);
	return magnitude ? dot / magnitude : 0;
};
