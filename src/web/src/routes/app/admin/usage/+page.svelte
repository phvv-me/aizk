<script lang="ts">
  import { formatBytes } from '$lib/api';
  import HorizontalBars from '$lib/components/HorizontalBars.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MeasuredAt from '$lib/components/MeasuredAt.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ReadingUnavailable from '$lib/components/ReadingUnavailable.svelte';
  import UsageChart from '$lib/components/UsageChart.svelte';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const usage = $derived(data.usage.value);
  // The reading measures every window the console offers, so changing period selects an
  // already-measured total instead of asking the API for another cross-scope aggregate.
  const periods = $derived(usage?.periods ?? []);
  let selected = $state(30);
  const period = $derived(periods.find((entry) => entry.days === selected) ?? periods[0] ?? null);
  const points = $derived(
    period
      ? (usage?.points ?? []).filter((point) => new Date(point.bucket) >= new Date(period.start))
      : []
  );
  const transfers = $derived(
    period
      ? [
          { label: 'Uploaded', value: period.summary.uploaded_bytes },
          { label: 'Downloaded', value: period.summary.downloaded_bytes }
        ]
      : []
  );
  const byActor = $derived(
    [...(usage?.by_actor ?? [])].sort((a, b) => b.finds + b.keeps - (a.finds + a.keeps))
  );
  const byScope = $derived(
    [...(usage?.by_scope ?? [])].sort((a, b) => b.finds + b.keeps - (a.finds + a.keeps))
  );
</script>

<PageHeader
  title="Usage"
  description="Durable successful operations across every caller and organization on this deployment."
/>

{#if !usage || !period}
  <ReadingUnavailable title="Usage" unreachable={data.usage.unreachable} />
{:else}
  <MeasuredAt measuredAt={usage.measured_at} stale={usage.stale} />

  <nav class="mb-6 flex flex-wrap gap-2" aria-label="Usage period">
    {#each periods as entry (entry.days)}
      <button
        type="button"
        onclick={() => (selected = entry.days)}
        aria-current={entry.days === period.days ? 'true' : undefined}
        class={`rounded-md border px-3 py-1.5 text-sm ${entry.days === period.days ? 'bg-primary text-primary-foreground border-primary' : 'hover:bg-accent'}`}
        >Last {entry.days} days</button
      >
    {/each}
  </nav>

  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Successful requests</Card.Description>
          <InfoTip
            label="What successful requests counts"
            text="One completed Find, Keep, Share, or artifact read by any caller. Failed calls and ordinary page views are excluded."
          />
        </div>
        <Card.Title class="text-3xl">{period.summary.requests.toLocaleString('en-US')}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Items handled</Card.Description>
          <InfoTip
            label="What items handled means"
            text="The number of evidence items, documents, or resources affected by successful operations. One request can handle several items."
          />
        </div>
        <Card.Title class="text-3xl">{period.summary.items.toLocaleString('en-US')}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Uploaded</Card.Description>
        <Card.Title class="text-3xl">{formatBytes(period.summary.uploaded_bytes)}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Downloaded</Card.Description>
        <Card.Title class="text-3xl">{formatBytes(period.summary.downloaded_bytes)}</Card.Title>
      </Card.Header>
    </Card.Root>
  </div>

  <div class="mb-6 grid gap-6 xl:grid-cols-[1.5fr_1fr]">
    <Card.Root>
      <Card.Content class="pt-6">
        <UsageChart {points} />
      </Card.Content>
    </Card.Root>
    <Card.Root>
      <Card.Content class="pt-6">
        <HorizontalBars
          title="Data transferred"
          description="Uploaded and downloaded bytes use their own scale so they are never visually mixed with operation counts."
          items={transfers}
        />
      </Card.Content>
    </Card.Root>
  </div>

  <Card.Root class="mb-6">
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Operation details</Card.Title>
        <InfoTip
          label="How to understand operation details"
          text="Counts for the selected window come from the immutable PostgreSQL usage ledger, aggregated across every scope by a worker holding platform-wide read."
        />
      </div>
      <Card.Description>
        Since {formatDateTime(period.start)}. Lifetime total {usage.lifetime.requests.toLocaleString(
          'en-US'
        )} successful requests.
      </Card.Description>
    </Card.Header>
    <Card.Content>
      <dl class="grid gap-5 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <dt class="text-muted-foreground text-xs">Finds</dt>
          <dd class="mt-1 text-xl">{period.summary.finds.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Keeps</dt>
          <dd class="mt-1 text-xl">{period.summary.keeps.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Files</dt>
          <dd class="mt-1 text-xl">{period.summary.files.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Shares</dt>
          <dd class="mt-1 text-xl">{period.summary.shares.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Artifact reads</dt>
          <dd class="mt-1 text-xl">{period.summary.artifact_reads.toLocaleString('en-US')}</dd>
        </div>
      </dl>
    </Card.Content>
  </Card.Root>

  <div class="grid gap-6 xl:grid-cols-2">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>By caller</Card.Title>
          <InfoTip
            label="Why these are lifetime totals"
            text="The per-caller and per-organization breakdowns are measured over the whole ledger rather than the selected window, so they answer who has used this deployment rather than who used it lately."
          />
        </div>
        <Card.Description>Lifetime totals for every authenticated caller.</Card.Description>
      </Card.Header>
      <Card.Content>
        {#if byActor.length === 0}
          <p class="text-muted-foreground text-sm">No usage has been recorded yet.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full min-w-[520px] text-left text-sm">
              <thead>
                <tr class="border-b">
                  <th class="pb-3 font-medium">Caller</th>
                  <th class="pb-3 font-medium">Finds</th>
                  <th class="pb-3 font-medium">Keeps</th>
                  <th class="pb-3 font-medium">Shares</th>
                  <th class="pb-3 font-medium">Reads</th>
                </tr>
              </thead>
              <tbody>
                {#each byActor as row (row.actor_id)}
                  <tr class="border-b last:border-0">
                    <td class="max-w-[10rem] truncate py-3 pr-4 font-mono text-xs"
                      >{row.actor_id}</td
                    >
                    <td class="py-3 pr-4">{row.finds.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.keeps.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.shares.toLocaleString('en-US')}</td>
                    <td class="py-3">{row.artifact_reads.toLocaleString('en-US')}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </Card.Content>
    </Card.Root>

    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>By organization</Card.Title>
          <InfoTip
            label="What a scope row means"
            text="The private or shared scope each operation touched, not the caller who made it. One operation reaching two organizations counts in both rows."
          />
        </div>
        <Card.Description>Lifetime totals for every scope operations reached.</Card.Description>
      </Card.Header>
      <Card.Content>
        {#if byScope.length === 0}
          <p class="text-muted-foreground text-sm">No usage has been recorded yet.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full min-w-[520px] text-left text-sm">
              <thead>
                <tr class="border-b">
                  <th class="pb-3 font-medium">Scope</th>
                  <th class="pb-3 font-medium">Finds</th>
                  <th class="pb-3 font-medium">Keeps</th>
                  <th class="pb-3 font-medium">Shares</th>
                  <th class="pb-3 font-medium">Reads</th>
                </tr>
              </thead>
              <tbody>
                {#each byScope as row (row.scope_id)}
                  <tr class="border-b last:border-0">
                    <td class="max-w-[10rem] truncate py-3 pr-4 font-mono text-xs"
                      >{row.scope_id}</td
                    >
                    <td class="py-3 pr-4">{row.finds.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.keeps.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.shares.toLocaleString('en-US')}</td>
                    <td class="py-3">{row.artifact_reads.toLocaleString('en-US')}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </Card.Content>
    </Card.Root>
  </div>
{/if}
