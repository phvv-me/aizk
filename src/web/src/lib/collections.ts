import { countBy, groupBy, orderBy, sumBy } from 'es-toolkit';

export type RankedCount = {
  label: string;
  value: number;
};

export type BucketTotals = {
  bucket: string;
  values: Record<string, number>;
  total: number;
};

/** Count labels with stable value-first and label-second ordering. */
export function rankedCounts<T>(
  items: readonly T[],
  label: (item: T) => string,
  limit?: number
): RankedCount[] {
  const ranked = orderBy(
    Object.entries(countBy(items, (item) => `:${label(item)}`)).map(([name, value]) => ({
      label: name.slice(1),
      value
    })),
    ['value', 'label'],
    ['desc', 'asc']
  );
  return limit === undefined ? ranked : ranked.slice(0, limit);
}

/** Aggregate values into deterministic bucket and series totals. */
export function bucketTotals<T>(
  items: readonly T[],
  bucket: (item: T) => string,
  series: (item: T) => string,
  value: (item: T) => number
): BucketTotals[] {
  const totals = Object.entries(groupBy(items, (item) => `:${bucket(item)}`)).map(
    ([bucketName, bucketItems]) => {
      const values = Object.fromEntries(
        Object.entries(groupBy(bucketItems, (item) => `:${series(item)}`)).map(
          ([seriesName, seriesItems]) => [seriesName.slice(1), sumBy(seriesItems, value)]
        )
      );
      return { bucket: bucketName.slice(1), values, total: sumBy(bucketItems, value) };
    }
  );
  return orderBy(totals, ['bucket'], ['asc']);
}
