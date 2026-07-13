// Entity-name canonicalization ported from graph/naming.py: unwrap wikilink and markdown
// link syntax to the visible label, drop path-like names entirely, then slugify to a
// space-separated lowercase key through a faithful port of python-slugify's default
// pipeline. PARITY SEAM: python-slugify transliterates any Unicode to ASCII through
// text_unidecode's full table and resolves named HTML entities; this port covers NFKD
// decomposition (which strips Latin diacritics), the common direct transliterations below,
// and numeric character references, so rarer scripts may normalize differently. The
// extract_check harness cross-checks both functions on shared strings.
import { casefold } from '../extract/grounding';

// Preserve visible labels while removing link syntax.
const WIKILINK = /\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g;
const MARKDOWN_LINK = /\[([^\]]+)\]\([^)]*\)/g;
const PATH_LIKE = /^(?:[a-z]+:\/\/|\/|\.{0,2}\/|[~.]\/)|\//;

const QUOTE_RUN = /'+/g;
const DECIMAL_REF = /&#(\d+);/g;
const HEX_REF = /&#x([\da-fA-F]+);/g;
const NUMBER_COMMA = /(?<=\d),(?=\d)/g;
const DISALLOWED = /[^-a-zA-Z0-9]+/g;
const DUPLICATE_DASH = /-{2,}/g;
const COMBINING_MARKS = /[\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u1dc0-\u1dff\ufe20-\ufe2f]/g;

const TRANSLITERATIONS: Record<string, string> = {
	ß: 'ss',
	ẞ: 'Ss',
	æ: 'ae',
	Æ: 'AE',
	œ: 'oe',
	Œ: 'OE',
	ø: 'o',
	Ø: 'O',
	đ: 'd',
	Đ: 'D',
	ð: 'd',
	Ð: 'D',
	þ: 'th',
	Þ: 'Th',
	ł: 'l',
	Ł: 'L'
};

const transliterate = (value: string): string =>
	[...value.normalize('NFKD').replace(COMBINING_MARKS, '')]
		.map((char) => TRANSLITERATIONS[char] ?? char)
		.join('');

// python-slugify's default pipeline with a space separator: pre-fold quotes to dashes,
// transliterate, resolve numeric references, lowercase, drop quotes, join digit groups,
// dash every disallowed run, collapse and strip dashes, then space-separate.
const slugify = (value: string): string =>
	transliterate(value.replace(QUOTE_RUN, '-'))
		.replace(DECIMAL_REF, (_, code: string) => String.fromCodePoint(Number(code)))
		.replace(HEX_REF, (_, code: string) => String.fromCodePoint(Number.parseInt(code, 16)))
		.normalize('NFKD')
		.toLowerCase()
		.replace(QUOTE_RUN, '')
		.replace(NUMBER_COMMA, '')
		.replace(DISALLOWED, '-')
		.replace(DUPLICATE_DASH, '-')
		.replace(/^-+|-+$/g, '')
		.replaceAll('-', ' ');

export const normalizeName = (name: string): string => {
	const unwrapped = name.replace(WIKILINK, '$1').replace(MARKDOWN_LINK, '$1').trim();
	if (PATH_LIKE.test(unwrapped)) return '';
	return slugify(casefold(unwrapped));
};
