import { describe, expect, it } from 'vitest';
import { webHref } from '../src/lib/utils';

describe('webHref', () => {
  it.each([
    ['py-csrf-protection-disabled', null],
    ['/app/pretend-source', null],
    ['javascript:alert(1)', null],
    [
      'https://codeql.github.com/codeql-query-help/python/',
      'https://codeql.github.com/codeql-query-help/python/'
    ]
  ])('accepts only absolute web links', (input, expected) => {
    expect(webHref(input)).toBe(expected);
  });
});
