import { describe, expect, it } from 'vitest';
import { formatAge, formatDuration, formatEtaRange } from '../src/lib/format';

describe('processing formatters', () => {
  it('formats bounded ETA ranges without false precision', () => {
    expect(formatEtaRange(600, 1200)).toBe('10 min to 20 min');
    expect(formatEtaRange(3600, 3600)).toBe('1 hr');
    expect(formatEtaRange(0, 0)).toBe('Complete');
    expect(formatEtaRange(null, 60)).toBe('ETA unavailable until more recent work completes');
  });

  it('formats durations for product copy', () => {
    expect(formatDuration(20)).toBe('under a minute');
    expect(formatDuration(3720)).toBe('1 hr 2 min');
  });
});

describe('formatAge', () => {
  it('says how old a reading is so a stale one is visible as stale', () => {
    const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
    expect(formatAge(minutesAgo(0))).toBe('just now');
    expect(formatAge(minutesAgo(7))).toBe('7 min ago');
    expect(formatAge(minutesAgo(180))).toBe('3 hr ago');
    expect(formatAge(minutesAgo(60 * 24 * 2))).toBe('2 days ago');
  });

  it('never claims a time it was not given', () => {
    expect(formatAge(null)).toBe('at an unknown time');
  });
});
