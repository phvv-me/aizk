<script lang="ts">
  import { Search } from '@lucide/svelte';
  import HorizontalBars from '$lib/components/HorizontalBars.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MemoryGraph from '$lib/components/MemoryGraph.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Input } from '$lib/components/ui/input';
  import { rankedCounts } from '$lib/collections';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const types = $derived(rankedCounts(data.subjects?.rows ?? [], (subject) => subject.type, 10));
</script>

<PageHeader
  title="Subjects"
  description="Explore the people, projects, places, concepts, and other named things in memory."
/>

<form method="GET" class="mb-6 flex flex-col gap-3 sm:flex-row" aria-label="Filter subjects">
  <div class="relative flex-1">
    <Search
      class="text-muted-foreground pointer-events-none absolute top-2.5 left-3 size-4"
      aria-hidden="true"
    />
    <Input
      name="search"
      value={data.search}
      placeholder="Search names or ontology types"
      class="pl-9"
    />
  </div>
  <Button type="submit" variant="secondary">Filter</Button>
</form>

{#if !data.subjects}
  <Card.Root>
    <Card.Header>
      <Card.Title>Subjects unavailable</Card.Title>
      <Card.Description
        >The subject catalog will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else}
  <div class="mb-6 grid gap-6 xl:grid-cols-[1fr_1.3fr]">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>{data.subjects.total.toLocaleString('en-US')} visible subjects</Card.Title>
          <InfoTip
            label="What a subject is"
            text="A subject is a scoped claim on a canonical named entity. The same canonical entity can appear in more than one scope without leaking private membership."
          />
        </div>
        <Card.Description>
          Finding counts show how many current claims touch each subject.
        </Card.Description>
      </Card.Header>
    </Card.Root>
    <Card.Root>
      <Card.Content class="pt-6">
        <HorizontalBars
          title="Subject types on this page"
          description="Ontology types describe what each named subject is. This chart covers the current page, while the table gives exact names and finding counts."
          items={types}
        />
      </Card.Content>
    </Card.Root>
  </div>

  {#if data.graph}
    <Card.Root class="mb-6">
      <Card.Content class="pt-6">
        <MemoryGraph graph={data.graph} />
      </Card.Content>
    </Card.Root>
  {/if}

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Subject catalog</Card.Title>
        <InfoTip
          label="How to read subject rows"
          text="Finding count is the current visible graph degree for this subject claim. Updated shows when the scoped claim last changed. Select the finding count to inspect matching claims."
        />
      </div>
      <Card.Description>Subjects with more visible findings appear first.</Card.Description>
    </Card.Header>
    <Card.Content>
      {#if data.subjects.rows.length === 0}
        <p class="text-muted-foreground text-sm">No subjects match this filter.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[680px] text-left text-sm">
            <caption class="sr-only">Visible subject claims</caption>
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Subject</th>
                <th class="pb-3 font-medium">Type</th>
                <th class="pb-3 text-right font-medium">Findings</th>
                <th class="pb-3 font-medium">Updated</th>
                <th class="pb-3 font-medium">Scope</th>
              </tr>
            </thead>
            <tbody>
              {#each data.subjects.rows as subject (subject.id)}
                <tr class="border-b last:border-0">
                  <td class="py-3 pr-4 font-medium">{subject.name}</td>
                  <td class="py-3 pr-4"><Badge variant="secondary">{subject.type}</Badge></td>
                  <td class="py-3 pr-4 text-right tabular-nums">
                    <a
                      href={`/findings?search=${encodeURIComponent(subject.name)}`}
                      class="text-primary font-medium hover:underline"
                      >{subject.finding_count.toLocaleString('en-US')}</a
                    >
                  </td>
                  <td class="text-muted-foreground py-3 pr-4"
                    >{formatDateTime(subject.updated_at)}</td
                  >
                  <td class="py-3"><ScopeBadges scopes={subject.scopes} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
      <div class="mt-5 flex items-center justify-between">
        {#if data.subjects.offset > 0}
          <a
            href={`?search=${encodeURIComponent(data.search)}&offset=${Math.max(0, data.subjects.offset - data.subjects.limit)}`}
            class="text-primary text-sm font-medium hover:underline">Previous page</a
          >
        {:else}
          <span></span>
        {/if}
        {#if data.subjects.offset + data.subjects.rows.length < data.subjects.total}
          <a
            href={`?search=${encodeURIComponent(data.search)}&offset=${data.subjects.offset + data.subjects.limit}`}
            class="text-primary text-sm font-medium hover:underline">Next page</a
          >
        {/if}
      </div>
    </Card.Content>
  </Card.Root>
{/if}
