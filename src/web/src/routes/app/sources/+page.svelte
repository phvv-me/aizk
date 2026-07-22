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
  import { webHref } from '$lib/utils';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const types = $derived(rankedCounts(data.sources?.rows ?? [], (source) => source.kind));
</script>

<PageHeader
  title="Sources"
  description="Browse the authored documents and preserved files that ground your memory."
/>

<nav class="mb-4 flex flex-wrap gap-2" aria-label="Source kind">
  {#each [{ value: 'all', label: 'All sources' }, { value: 'document', label: 'Documents' }, { value: 'file', label: 'Files' }] as option (option.value)}
    <a
      href={appHref(appRoutes.sources, {
        origin: option.value === 'all' ? undefined : option.value,
        search: data.search
      })}
      aria-current={data.origin === option.value ? 'page' : undefined}
      class:font-semibold={data.origin === option.value}
      class="hover:bg-accent rounded-md border px-3 py-1.5 text-sm">{option.label}</a
    >
  {/each}
</nav>

<form method="GET" class="mb-6 flex flex-col gap-3 sm:flex-row" aria-label="Filter sources">
  <div class="relative flex-1">
    <Search
      class="text-muted-foreground pointer-events-none absolute top-2.5 left-3 size-4"
      aria-hidden="true"
    />
    <Input
      name="search"
      value={data.search}
      placeholder="Search titles or source links"
      class="pl-9"
    />
    {#if data.origin !== 'all'}
      <input type="hidden" name="origin" value={data.origin} />
    {/if}
  </div>
  <Button type="submit" variant="secondary">Filter</Button>
</form>

{#if !data.sources}
  <Card.Root>
    <Card.Header>
      <Card.Title>Sources unavailable</Card.Title>
      <Card.Description
        >The source catalog will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else}
  <div class="mb-6 grid gap-6 xl:grid-cols-[1fr_1.4fr]">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>{data.sources.total.toLocaleString('en-US')} visible sources</Card.Title>
          <InfoTip
            label="What a source is"
            text="A source grounds recalled evidence. Documents are remembered text. Files are preserved originals that AIZK converts into searchable text. Findings and subjects inherit their provenance and visibility from these sources."
          />
        </div>
        <Card.Description>
          Showing {data.sources.rows.length.toLocaleString('en-US')} sources on this page.
        </Card.Description>
      </Card.Header>
    </Card.Root>
    <Card.Root>
      <Card.Content class="pt-6">
        <HorizontalBars
          title="Source types on this page"
          description="This chart groups the currently displayed sources by their declared ontology type. Use the table below for the full source details."
          items={types}
        />
      </Card.Content>
    </Card.Root>
  </div>

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Source catalog</Card.Title>
        <InfoTip
          label="How to read the source catalog"
          text="Observed is when the source says it was true or published. Updated is when AIZK last stored this source. Scope badges show who can see it."
        />
      </div>
      <Card.Description>Newest stored sources appear first.</Card.Description>
    </Card.Header>
    <Card.Content>
      {#if data.sources.rows.length === 0}
        <p class="text-muted-foreground text-sm">No sources match this filter.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[760px] text-left text-sm">
            <caption class="sr-only">Visible source documents</caption>
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Source</th>
                <th class="pb-3 font-medium">Origin</th>
                <th class="pb-3 font-medium">Content type</th>
                <th class="pb-3 font-medium">Observed</th>
                <th class="pb-3 font-medium">Updated</th>
                <th class="pb-3 font-medium">Scope</th>
              </tr>
            </thead>
            <tbody>
              {#each data.sources.rows as source (source.id)}
                {@const href = webHref(source.source_uri)}
                <tr class="border-b last:border-0">
                  <td class="max-w-sm py-3 pr-4">
                    {#if href}
                      <a
                        {href}
                        target="_blank"
                        rel="noreferrer"
                        class="font-medium hover:underline"
                      >
                        {source.title}
                      </a>
                    {:else}
                      <span class="font-medium">{source.title}</span>
                    {/if}
                    {#if source.source_uri}
                      <p class="text-muted-foreground mt-1 truncate text-xs">{source.source_uri}</p>
                    {/if}
                  </td>
                  <td class="py-3 pr-4">
                    <Badge variant="secondary">
                      {source.origin === 'file' ? 'File' : 'Document'}
                    </Badge>
                  </td>
                  <td class="py-3 pr-4">{source.kind}</td>
                  <td class="text-muted-foreground py-3 pr-4"
                    >{formatDateTime(source.observed_at)}</td
                  >
                  <td class="text-muted-foreground py-3 pr-4"
                    >{formatDateTime(source.updated_at)}</td
                  >
                  <td class="py-3"><ScopeBadges scopes={source.scopes} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      <div class="mt-5 flex items-center justify-between">
        {#if data.sources.offset > 0}
          <a
            href={appHref(appRoutes.sources, {
              search: data.search,
              origin: data.origin === 'all' ? undefined : data.origin,
              offset: Math.max(0, data.sources.offset - data.sources.limit)
            })}
            class="text-primary text-sm font-medium hover:underline">Previous page</a
          >
        {:else}
          <span></span>
        {/if}
        {#if data.sources.offset + data.sources.rows.length < data.sources.total}
          <a
            href={appHref(appRoutes.sources, {
              search: data.search,
              origin: data.origin === 'all' ? undefined : data.origin,
              offset: data.sources.offset + data.sources.limit
            })}
            class="text-primary text-sm font-medium hover:underline">Next page</a
          >
        {/if}
      </div>
    </Card.Content>
  </Card.Root>
{/if}
