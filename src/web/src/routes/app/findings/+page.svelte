<script lang="ts">
  import { Search } from '@lucide/svelte';
  import HorizontalBars from '$lib/components/HorizontalBars.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Input } from '$lib/components/ui/input';
  import { rankedCounts } from '$lib/collections';
  import { formatDateTime } from '$lib/format';
  import { appHref, appRoutes } from '$lib/routes';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const predicates = $derived(
    rankedCounts(data.findings?.rows ?? [], (finding) => finding.predicate, 8)
  );
</script>

<PageHeader
  title="Findings"
  description="Inspect current claims extracted from sources, with their subjects and provenance."
/>

<form method="GET" class="mb-6 flex flex-col gap-3 sm:flex-row" aria-label="Filter findings">
  <div class="relative flex-1">
    <Search
      class="text-muted-foreground pointer-events-none absolute top-2.5 left-3 size-4"
      aria-hidden="true"
    />
    <Input
      name="search"
      value={data.search}
      placeholder="Search statements, predicates, or subjects"
      class="pl-9"
    />
  </div>
  <Button type="submit" variant="secondary">Filter</Button>
</form>

{#if !data.findings}
  <Card.Root>
    <Card.Header>
      <Card.Title>Findings unavailable</Card.Title>
      <Card.Description
        >The finding catalog will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else}
  <div class="mb-6 grid gap-6 xl:grid-cols-[1fr_1.4fr]">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>{data.findings.total.toLocaleString('en-US')} current findings</Card.Title>
          <InfoTip
            label="What a finding is"
            text="A finding is one current claim extracted from a source. It links a subject to a statement and often to another subject through a controlled predicate."
          />
        </div>
        <Card.Description>
          Findings are current projections. Superseded history remains in the durable store but is
          not shown here.
        </Card.Description>
      </Card.Header>
    </Card.Root>
    <Card.Root>
      <Card.Content class="pt-6">
        <HorizontalBars
          title="Predicates on this page"
          description="Predicates describe how subjects relate. This chart shows the most common relation types in the current page of findings."
          items={predicates}
        />
      </Card.Content>
    </Card.Root>
  </div>

  <div class="space-y-4">
    {#if data.findings.rows.length === 0}
      <Card.Root>
        <Card.Content class="py-6">
          <p class="text-muted-foreground text-sm">No findings match this filter.</p>
        </Card.Content>
      </Card.Root>
    {:else}
      {#each data.findings.rows as finding (finding.id)}
        <Card.Root>
          <Card.Header>
            <div class="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{finding.predicate}</Badge>
              <a
                href={appHref(appRoutes.subjects, { search: finding.subject_name })}
                class="font-medium hover:underline">{finding.subject_name}</a
              >
              {#if finding.object_name}
                <span class="text-muted-foreground text-sm">to</span>
                <a
                  href={appHref(appRoutes.subjects, { search: finding.object_name })}
                  class="font-medium hover:underline">{finding.object_name}</a
                >
              {/if}
            </div>
            <Card.Description>Recorded {formatDateTime(finding.recorded_at)}</Card.Description>
            <Card.Action><ScopeBadges scopes={finding.scopes} /></Card.Action>
          </Card.Header>
          <Card.Content>
            <p class="leading-relaxed">{finding.statement}</p>
            {#if finding.source_title}
              <p class="text-muted-foreground mt-3 text-xs">
                Grounded in
                <a
                  href={appHref(appRoutes.sources, { search: finding.source_title })}
                  class="text-primary hover:underline">{finding.source_title}</a
                >
              </p>
            {/if}
          </Card.Content>
        </Card.Root>
      {/each}
    {/if}
  </div>

  <div class="mt-6 flex items-center justify-between">
    {#if data.findings.offset > 0}
      <a
        href={`?search=${encodeURIComponent(data.search)}&offset=${Math.max(0, data.findings.offset - data.findings.limit)}`}
        class="text-primary text-sm font-medium hover:underline">Previous page</a
      >
    {:else}
      <span></span>
    {/if}
    {#if data.findings.offset + data.findings.rows.length < data.findings.total}
      <a
        href={`?search=${encodeURIComponent(data.search)}&offset=${data.findings.offset + data.findings.limit}`}
        class="text-primary text-sm font-medium hover:underline">Next page</a
      >
    {/if}
  </div>
{/if}
