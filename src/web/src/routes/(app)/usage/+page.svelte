<script lang="ts">
  import { formatBytes } from '$lib/api';
  import HorizontalBars from '$lib/components/HorizontalBars.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import UsageChart from '$lib/components/UsageChart.svelte';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const periods = [7, 30, 90, 365];
  const transfers = $derived(
    data.usage
      ? [
          { label: 'Uploaded', value: data.usage.summary.uploaded_bytes },
          { label: 'Downloaded', value: data.usage.summary.downloaded_bytes }
        ]
      : []
  );
</script>

<PageHeader
  title="Usage"
  description="Inspect durable successful operation and transfer history that survives service restarts."
/>

<nav class="mb-6 flex flex-wrap gap-2" aria-label="Usage period">
  {#each periods as period (period)}
    <a
      href={`?days=${period}`}
      aria-current={data.days === period ? 'page' : undefined}
      class={`rounded-md border px-3 py-1.5 text-sm ${data.days === period ? 'bg-primary text-primary-foreground border-primary' : 'hover:bg-accent'}`}
      >Last {period} days</a
    >
  {/each}
</nav>

{#if !data.usage}
  <Card.Root>
    <Card.Header>
      <Card.Title>Usage unavailable</Card.Title>
      <Card.Description>Usage history will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else}
  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Successful requests</Card.Description>
          <InfoTip
            label="What successful requests counts"
            text="One completed Recall, Remember, Share, or artifact read. Failed calls and ordinary page views are excluded."
          />
        </div>
        <Card.Title class="text-3xl"
          >{data.usage.summary.requests.toLocaleString('en-US')}</Card.Title
        >
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
        <Card.Title class="text-3xl">{data.usage.summary.items.toLocaleString('en-US')}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Uploaded</Card.Description>
          <InfoTip
            label="What uploaded measures"
            text="Transport bytes received for successful file ingestion. Source URI downloads can be larger than their short request payload and are tracked separately in storage."
          />
        </div>
        <Card.Title class="text-3xl">{formatBytes(data.usage.summary.uploaded_bytes)}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Downloaded</Card.Description>
          <InfoTip
            label="What downloaded measures"
            text="Original artifact bytes returned through successful authorized resource reads."
          />
        </div>
        <Card.Title class="text-3xl">{formatBytes(data.usage.summary.downloaded_bytes)}</Card.Title>
      </Card.Header>
    </Card.Root>
  </div>

  <div class="mb-6 grid gap-6 xl:grid-cols-[1.5fr_1fr]">
    <Card.Root>
      <Card.Content class="pt-6">
        <UsageChart points={data.usage.points} />
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

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Operation details</Card.Title>
        <InfoTip
          label="How to understand operation details"
          text="The selected-period counts below come from the immutable PostgreSQL usage ledger. Remember and Share activity can come from connected clients even though source intake is not shown in the web navigation."
        />
      </div>
      <Card.Description>
        Recorded through {formatDateTime(data.usage.recorded_through)}. Lifetime total {data.usage.lifetime.requests.toLocaleString(
          'en-US'
        )} successful requests.
      </Card.Description>
    </Card.Header>
    <Card.Content>
      <dl class="grid gap-5 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <dt class="text-muted-foreground text-xs">Recalls</dt>
          <dd class="mt-1 text-xl">{data.usage.summary.recalls.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Remembers</dt>
          <dd class="mt-1 text-xl">{data.usage.summary.remembers.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Files</dt>
          <dd class="mt-1 text-xl">{data.usage.summary.files.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Shares</dt>
          <dd class="mt-1 text-xl">{data.usage.summary.shares.toLocaleString('en-US')}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground text-xs">Artifact reads</dt>
          <dd class="mt-1 text-xl">{data.usage.summary.artifact_reads.toLocaleString('en-US')}</dd>
        </div>
      </dl>
    </Card.Content>
  </Card.Root>
{/if}
