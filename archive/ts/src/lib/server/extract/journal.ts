// Journal parsing ported from the Python extract/journal.py: dated bullet entries become
// observation facts against the note's own title entity, and #area/#project tags declare
// the note's structural type.
import { AREA, CONCEPT, OBSERVES, PROJECT } from './ontology';
import type { ExtractedEntity, TimedFact } from './models';

// Dated entries may include a parenthesized label after the date.
const JOURNAL_LINE = /^-\s*(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?:\s*(.+)$/gm;

const AREA_TAG = /(?<!\w)#area(?!\w)/i;
const PROJECT_TAG = /(?<!\w)#project(?!\w)/i;

// Whether text contains at least one dated journal entry. The shared pattern is global for
// matchAll, so the probe resets its cursor on both sides of the test.
export const hasJournalEntries = (text: string): boolean => {
	JOURNAL_LINE.lastIndex = 0;
	const found = JOURNAL_LINE.test(text);
	JOURNAL_LINE.lastIndex = 0;
	return found;
};

// The structural type a note's own tags declare, Area or Project, else null for an
// ordinary note the extractor is left to characterize.
export const declaredType = (text: string): string | null => {
	if (AREA_TAG.test(text)) return AREA;
	if (PROJECT_TAG.test(text)) return PROJECT;
	return null;
};

// The note's own title as an entity, typed by the structural tag it declares or Concept.
export const titleEntity = (title: string, declared: string | null): ExtractedEntity => ({
	name: title,
	type: declared ?? CONCEPT,
	suggestedType: null
});

// Parse a chunk's dated journal lines into facts logged against the note's title entity.
export const journalFacts = (chunkText: string, title: string): TimedFact[] => {
	JOURNAL_LINE.lastIndex = 0;
	return [...chunkText.matchAll(JOURNAL_LINE)].map(([, dateText, statement]) => {
		const [year, month, day] = dateText.split('-').map(Number);
		return {
			subject: title,
			predicate: OBSERVES,
			object: '',
			statement: statement.trim(),
			quote: null,
			validFrom: new Date(Date.UTC(year, month - 1, day)),
			validTo: null,
			kind: 'world' as const
		};
	});
};
