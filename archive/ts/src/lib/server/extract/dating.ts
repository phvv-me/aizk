// Fact dating ported from the Python extract/dating.py. PARITY SEAM: Python parses with
// the dateparser library (absolute-time parser, STRICT_PARSING, DATE_ORDER=YMD, prefer
// past, timezone-aware in the machine's local zone). This port recognizes only the strict
// numeric YMD forms — YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD, with an optional HH:MM[:SS] time
// and optional Z or ±HH:MM offset — and returns null for everything else (month names,
// locale phrases, dateparser's timezone-word quirks), so an undated fact keeps the
// document-fallback date. Zoneless matches localize to the machine zone like dateparser.
import type { TimedFact } from './models';

const DIRECT = new RegExp(
	'^(\\d{4})([-/.])(\\d{2})\\2(\\d{2})' +
		'(?:[T ](\\d{2}):(\\d{2})(?::(\\d{2}))?)?' +
		'(Z|[+-]\\d{2}:?\\d{2})?$'
);
const EMBEDDED = /\d{4}[-/.]\d{2}[-/.]\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:?\d{2})?/g;

const fromParts = (match: RegExpMatchArray): Date | null => {
	const [, year, , month, day, hour, minute, second, zone] = match;
	const y = Number(year);
	const mo = Number(month);
	const d = Number(day);
	const h = Number(hour ?? '0');
	const mi = Number(minute ?? '0');
	const s = Number(second ?? '0');
	if (mo < 1 || mo > 12 || d < 1 || h > 23 || mi > 59 || s > 59) return null;
	if (d > new Date(Date.UTC(y, mo, 0)).getUTCDate()) return null;
	if (zone === undefined) {
		// No offset: localize to the machine zone, matching dateparser's tz-aware return.
		return new Date(y, mo - 1, d, h, mi, s);
	}
	const offsetMinutes =
		zone === 'Z'
			? 0
			: (zone.startsWith('-') ? -1 : 1) *
				(Number(zone.slice(1, 3)) * 60 + Number(zone.slice(-2)));
	return new Date(Date.UTC(y, mo - 1, d, h, mi, s) - offsetMinutes * 60_000);
};

const parseDirect = (text: string): Date | null => {
	const match = text.trim().match(DIRECT);
	return match === null ? null : fromParts(match);
};

// Parse a date out of free text with no LLM call, or return null when the text names none.
export const parseDate = (text: string): Date | null => {
	if (!text) return null;
	const direct = parseDirect(text);
	if (direct !== null) return direct;
	const found = [...text.matchAll(EMBEDDED)]
		.map((embedded) => parseDirect(embedded[0]))
		.filter((date): date is Date => date !== null);
	if (!found.length) return null;
	return found.reduce((earliest, date) => (date < earliest ? date : earliest));
};

// Resolve one fact's valid_from from its own text with no LLM call.
export const resolveValidFrom = (explicit: string | null, statement: string): Date | null =>
	parseDate(explicit ?? '') ?? parseDate(statement);

// Fill every still-undated fact's valid_from with the source document's own timestamp.
export const withDocumentFallback = (facts: TimedFact[], documentCreatedAt: Date): TimedFact[] =>
	facts.map((fact) =>
		fact.validFrom !== null ? fact : { ...fact, validFrom: documentCreatedAt }
	);
