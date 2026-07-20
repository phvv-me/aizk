<script lang="ts">
  import type { UsagePoint } from '$lib/api';
  import { bucketTotals } from '$lib/collections';
  import InfoTip from './InfoTip.svelte';

  let { points }: { points: UsagePoint[] } = $props();

  const categories = [
    { key: 'recall', label: 'Recall', color: 'var(--series-1)' },
    { key: 'remember', label: 'Remember', color: 'var(--series-2)' },
    { key: 'share', label: 'Share', color: 'var(--series-3)' },
    { key: 'artifact_read', label: 'Artifact read', color: 'var(--series-4)' }
  ] as const;

  function category(operation: string): (typeof categories)[number]['key'] {
    return operation.startsWith('remember_')
      ? 'remember'
      : (operation as (typeof categories)[number]['key']);
  }

  const days = $derived(
    bucketTotals(
      points,
      (point) => point.bucket.slice(0, 10),
      (point) => category(point.operation),
      (point) => point.requests
    ).map(({ bucket: day, values, total }) => ({ day, values, total }))
  );
  const maximum = $derived(Math.max(1, ...days.map((day) => day.total)));
</script>

<figure class="viz-root space-y-4">
  <figcaption>
    <div class="flex items-center gap-2 font-medium">
      Successful operations over time
      <InfoTip
        label="How to read successful operations"
        text="Each column is one UTC day. The height is the number of successful AIZK operations. Remember combines text and file ingestion from connected clients. Failed requests and page views are not counted."
      />
    </div>
    <p class="text-muted-foreground mt-1 text-sm">
      Daily successful Recall, Remember, Share, and artifact read operations.
    </p>
  </figcaption>
  <div class="flex flex-wrap gap-4 text-xs" aria-label="Operation legend">
    {#each categories as item (item.key)}
      <span class="flex items-center gap-2">
        <span class="size-2.5 rounded-sm" style={`background: ${item.color}`}></span>
        {item.label}
      </span>
    {/each}
  </div>
  {#if days.length === 0}
    <p class="text-muted-foreground text-sm">
      No successful operations were recorded in this period.
    </p>
  {:else}
    <div class="overflow-x-auto pb-2">
      <div class="flex h-56 min-w-max items-end gap-2 border-b px-1" aria-label="Daily usage chart">
        {#each days as day (day.day)}
          <div class="flex w-6 flex-col items-center gap-2">
            <div class="flex h-48 w-6 flex-col-reverse justify-start gap-0.5">
              {#each categories as item (item.key)}
                {@const value = day.values[item.key] ?? 0}
                {#if value > 0}
                  <div
                    class="min-h-1 rounded-sm"
                    style={`height: ${(value / maximum) * 100}%; background: ${item.color}`}
                    title={`${day.day} ${item.label} ${value}`}
                  ></div>
                {/if}
              {/each}
            </div>
            <span class="text-muted-foreground -rotate-45 text-[10px] whitespace-nowrap"
              >{day.day.slice(5)}</span
            >
          </div>
        {/each}
      </div>
    </div>
    <details class="text-sm">
      <summary class="text-muted-foreground hover:text-foreground cursor-pointer"
        >Show data table</summary
      >
      <div class="mt-3 overflow-x-auto">
        <table class="w-full text-left">
          <caption class="sr-only">Successful operations by UTC day</caption>
          <thead>
            <tr class="border-b">
              <th class="py-2 font-medium">UTC day</th>
              {#each categories as item (item.key)}
                <th class="py-2 text-right font-medium">{item.label}</th>
              {/each}
              <th class="py-2 text-right font-medium">Total</th>
            </tr>
          </thead>
          <tbody>
            {#each days as day (day.day)}
              <tr class="border-b last:border-0">
                <td class="py-2">{day.day}</td>
                {#each categories as item (item.key)}
                  <td class="py-2 text-right tabular-nums">{day.values[item.key] ?? 0}</td>
                {/each}
                <td class="py-2 text-right font-medium tabular-nums">{day.total}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </details>
  {/if}
</figure>
