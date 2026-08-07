import { describe, expect, it } from 'vitest';
import { enumParam, stringParam } from '../src/lib/query';

describe('stringParam', () => {
  it('trims a present value', () => {
    expect(stringParam('  needle  ')).toBe('needle');
  });

  it('treats blank, whitespace-only, and absent values as unfiltered', () => {
    expect(stringParam('')).toBeUndefined();
    expect(stringParam('   ')).toBeUndefined();
    expect(stringParam(null)).toBeUndefined();
  });
});

describe('enumParam', () => {
  const kinds = ['recall', 'share'] as const;

  it('accepts a value present in the allowed set', () => {
    expect(enumParam('recall', kinds)).toBe('recall');
  });

  it('rejects a value outside the allowed set and an absent value', () => {
    expect(enumParam('remember_text', kinds)).toBeUndefined();
    expect(enumParam(null, kinds)).toBeUndefined();
  });
});
