// Deterministic content ids ported exactly from graph/ids.py: uuid5 over the same private
// namespace and unit-separator-joined normalized fields, so the same entity or fact minted
// from either server lands on the byte-identical content row.
import { v5 as uuid5 } from 'uuid';

import { casefold, collapseWhitespace } from '../extract/grounding';
import { normalizeName } from './naming';

const NAMESPACE = 'a12c0de0-0000-5000-8000-a12c00000000';
const DELIMITER = '\x1f';

// Fold a field to its canonical form before hashing, lowercased with collapsed whitespace.
const normalize = (value: string): string => casefold(collapseWhitespace(value));

export const entityId = (name: string, type: string): string =>
	uuid5([normalize(type), normalizeName(name)].join(DELIMITER), NAMESPACE);

export const factId = (
	subject: string,
	predicate: string,
	object: string,
	statement: string
): string =>
	uuid5(
		[normalize(subject), normalize(predicate), normalize(object), normalize(statement)].join(
			DELIMITER
		),
		NAMESPACE
	);
