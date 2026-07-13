// Quote grounding ported from the Python extract/grounding.py: locate a model-emitted
// quote inside its source chunk as code-point offsets, retrying case- and whitespace-
// insensitively. Python folds with str.casefold; the casefold here lowercases per code
// point and applies the explicit one-to-many full foldings (ß→ss and the Latin ligatures),
// which the harness cross-checks against the Python function on shared test strings.

// Exactly the code points Python's str.isspace accepts.
const SPACE = /[\t-\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
const SPACE_RUN = new RegExp(`${SPACE.source}+`, 'gu');

// One-to-many full case foldings JS toLowerCase does not perform.
const FULL_FOLDS: Record<string, string> = {
	ß: 'ss',
	ẞ: 'ss',
	ς: 'σ',
	µ: 'μ',
	ﬀ: 'ff',
	ﬁ: 'fi',
	ﬂ: 'fl',
	ﬃ: 'ffi',
	ﬄ: 'ffl',
	ﬅ: 'st',
	ﬆ: 'st',
	ŉ: 'ʼn'
};

// Fold one code point the way Python str.casefold does for the covered repertoire.
export const casefoldChar = (char: string): string => FULL_FOLDS[char] ?? char.toLowerCase();

// Fold a whole string code point by code point.
export const casefold = (text: string): string => [...text].map(casefoldChar).join('');

// Split on Python str.split() whitespace and rejoin with single spaces.
export const collapseWhitespace = (text: string): string =>
	text.split(SPACE_RUN).filter(Boolean).join(' ');

// Casefold text with whitespace runs collapsed, keeping each position's source offset.
// Offsets count code points, matching Python string indexing; the offsets array carries one
// entry per UTF-16 unit of the folded text so indexOf positions map directly.
export const normalizedMap = (text: string): [string, number[]] => {
	const folded: string[] = [];
	const offsets: number[] = [];
	let pendingSpace = false;
	let offset = 0;
	for (const char of text) {
		if (SPACE.test(char)) {
			pendingSpace = folded.length > 0;
			offset += 1;
			continue;
		}
		if (pendingSpace) {
			folded.push(' ');
			offsets.push(offset - 1);
			pendingSpace = false;
		}
		// One offset per folded unit: a single source char may casefold to several.
		for (const piece of casefoldChar(char)) {
			for (let unit = 0; unit < piece.length; unit += 1) {
				folded.push(piece[unit]);
				offsets.push(offset);
			}
		}
		offset += 1;
	}
	return [folded.join(''), offsets];
};

const codePoints = (text: string): number => [...text].length;

// Locate a model-emitted quote in its source text as [start, end) code-point offsets. An
// exact match wins; otherwise matching retries case- and whitespace-insensitively, the two
// ways a model most often mangles a "verbatim" excerpt. A quote that still cannot be found
// returns null and the fact simply carries no grounding.
export const quoteInterval = (quote: string | null, text: string): [number, number] | null => {
	if (quote === null) return null;
	const stripped = quote.replace(new RegExp(`^${SPACE.source}+|${SPACE.source}+$`, 'gu'), '');
	if (!stripped) return null;
	const exact = text.indexOf(stripped);
	if (exact >= 0) {
		const start = codePoints(text.slice(0, exact));
		return [start, start + codePoints(stripped)];
	}
	const [foldedText, offsets] = normalizedMap(text);
	const foldedQuote = casefold(stripped).replace(SPACE_RUN, ' ');
	const start = foldedText.indexOf(foldedQuote);
	if (start < 0) return null;
	const last = offsets[start + foldedQuote.length - 1];
	return [offsets[start], last + 1];
};
