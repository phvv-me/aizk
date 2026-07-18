import { describe, expect, it } from 'vitest';
import { formatBytes } from '../src/lib/api';

describe('formatBytes', () => {
  it('keeps small counts in bytes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('scales through the binary units with one decimal under ten', () => {
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(1024 ** 3 * 42)).toBe('42 GB');
    expect(formatBytes(1024 ** 4 * 3000)).toBe('3000 TB');
  });
});
