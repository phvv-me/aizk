import { describe, expect, it } from 'vitest';
import { formatDuration, formatEtaRange, sentence } from '../src/lib/format';

describe('processing formatters', () => {
  it('formats bounded ETA ranges without false precision', () => {
    expect(formatEtaRange(600, 1200)).toBe('10 min to 20 min');
    expect(formatEtaRange(3600, 3600)).toBe('1 hr');
    expect(formatEtaRange(0, 0)).toBe('Complete');
    expect(formatEtaRange(null, 60)).toBe('ETA unavailable until more recent work completes');
  });

  it('formats durations and machine names for product copy', () => {
    expect(formatDuration(20)).toBe('under a minute');
    expect(formatDuration(3720)).toBe('1 hr 2 min');
    expect(sentence('graph_projection')).toBe('Graph projection');
  });
});
