<script lang="ts">
  import { formatBytes } from '$lib/api';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import * as Card from '$lib/components/ui/card';
  import { webHref } from '$lib/utils';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const overview = $derived(data.overview);
</script>

{#snippet metric(label: string, value: string)}
  <Card.Root class="gap-1 py-4">
    <Card.Header class="px-4">
      <Card.Description>{label}</Card.Description>
      <Card.Title class="text-2xl tabular-nums">{value}</Card.Title>
    </Card.Header>
  </Card.Root>
{/snippet}

<PageHeader title="Dashboard" description="Knowledge and usage totals with your latest sources." />

{#if !overview}
  <Card.Root>
    <Card.Header>
      <Card.Title>Overview unavailable</Card.Title>
      <Card.Description>
        Totals and recent sources will appear once the AIZK API answers again.
      </Card.Description>
    </Card.Header>
  </Card.Root>
{:else}
  <section aria-label="Knowledge totals" class="mb-8">
    <h2 class="text-muted-foreground mb-3 text-sm font-medium tracking-wide uppercase">
      Knowledge
    </h2>
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {@render metric('Sources', overview.totals.sources.toLocaleString('en-US'))}
      {@render metric('Findings', overview.totals.findings.toLocaleString('en-US'))}
      {@render metric('Subjects', overview.totals.subjects.toLocaleString('en-US'))}
      {@render metric('Themes', overview.totals.themes.toLocaleString('en-US'))}
    </div>
  </section>

  <section aria-label="Usage totals" class="mb-8">
    <h2 class="text-muted-foreground mb-3 text-sm font-medium tracking-wide uppercase">Usage</h2>
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      {@render metric('Recalls', overview.usage.recalls.toLocaleString('en-US'))}
      {@render metric('Remembers', overview.usage.remembers.toLocaleString('en-US'))}
      {@render metric('Files', overview.usage.files.toLocaleString('en-US'))}
      {@render metric('Uploaded', formatBytes(overview.usage.uploaded_bytes))}
      {@render metric('Downloaded', formatBytes(overview.usage.downloaded_bytes))}
    </div>
  </section>

  <section aria-label="Recent sources">
    <Card.Root>
      <Card.Header>
        <Card.Title>Recent sources</Card.Title>
        <Card.Description>The latest documents written into your memory.</Card.Description>
      </Card.Header>
      <Card.Content>
        {#if overview.recent_sources.length === 0}
          <p class="text-muted-foreground text-sm">
            Nothing remembered yet. Start on the Sources page.
          </p>
        {:else}
          <ul class="divide-border divide-y">
            {#each overview.recent_sources as source, index (index)}
              {@const href = webHref(source.source_uri)}
              <li class="flex flex-wrap items-center gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0">
                <div class="min-w-0 flex-1">
                  {#if href}
                    <a
                      {href}
                      target="_blank"
                      rel="noreferrer"
                      class="hover:text-primary block truncate text-sm font-medium underline-offset-4 hover:underline"
                    >
                      {source.title}
                    </a>
                  {:else}
                    <p class="truncate text-sm font-medium">{source.title}</p>
                  {/if}
                  <p class="text-muted-foreground text-xs">{source.kind} · {source.date}</p>
                </div>
                <ScopeBadges scopes={source.scopes} />
              </li>
            {/each}
          </ul>
        {/if}
      </Card.Content>
    </Card.Root>
  </section>
{/if}
