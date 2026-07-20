import { describe, expect, it } from 'vitest';
import { bucketTotals, rankedCounts } from '../src/lib/collections';

describe('rankedCounts', () => {
  it('counts values with deterministic tie ordering and a limit', () => {
    const items = [
      { type: 'project' },
      { type: 'person' },
      { type: 'project' },
      { type: 'concept' },
      { type: 'person' }
    ];

    expect(rankedCounts(items, (item) => item.type, 2)).toEqual([
      { label: 'person', value: 2 },
      { label: 'project', value: 2 }
    ]);
  });

  it('returns every count when no limit is provided', () => {
    expect(rankedCounts(['note', '__proto__', 'note'], (value) => value)).toEqual([
      { label: 'note', value: 2 },
      { label: '__proto__', value: 1 }
    ]);
  });
});

describe('bucketTotals', () => {
  it('groups repeated series and orders buckets deterministically', () => {
    const points = [
      { day: '2026-07-20', operation: 'remember', requests: 2 },
      { day: '2026-07-19', operation: 'recall', requests: 3 },
      { day: '2026-07-20', operation: 'remember', requests: 4 },
      { day: '2026-07-20', operation: '__proto__', requests: 1 }
    ];

    expect(
      bucketTotals(
        points,
        (point) => point.day,
        (point) => point.operation,
        (point) => point.requests
      )
    ).toEqual([
      { bucket: '2026-07-19', values: { recall: 3 }, total: 3 },
      { bucket: '2026-07-20', values: { remember: 6, ['__proto__']: 1 }, total: 7 }
    ]);
  });
});
