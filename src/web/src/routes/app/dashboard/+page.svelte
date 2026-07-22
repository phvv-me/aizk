<script lang="ts">
  import { onMount } from 'svelte';
  import type { ProcessingReport } from '$lib/api';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import StageProgress from '$lib/components/StageProgress.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatEtaRange } from '$lib/format';
  import { ProcessingEvents, type ProcessingConnection } from '$lib/processing-events';
  import { appHref, appRoutes } from '$lib/routes';
  import { webHref } from '$lib/utils';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();
  let liveProcessing = $state<ProcessingReport | null>(null);
  let processingConnection = $state<ProcessingConnection>('connecting');

  const overview = $derived(data.overview);
  const processing = $derived(liveProcessing ?? data.processing);
  const usage = $derived(data.usage);
  const conversion = $derived(processing?.stages.find((stage) => stage.key === 'conversion'));
  const projection = $derived(processing?.stages.find((stage) => stage.key === 'graph_projection'));

  const metrics = $derived(
    overview
      ? [
          {
            label: 'Documents',
            value: overview.totals.documents,
            href: appHref(appRoutes.sources, { origin: 'document' }),
            help: 'Text remembered directly through AIZK clients.'
          },
          {
            label: 'Files',
            value: overview.totals.files,
            href: appHref(appRoutes.sources, { origin: 'file' }),
            help: 'Uploaded or fetched originals preserved by AIZK.'
          },
          {
            label: 'Findings',
            value: overview.totals.findings,
            href: appRoutes.findings,
            help: 'Current claims extracted from source text and retained with provenance.'
          },
          {
            label: 'Subjects',
            value: overview.totals.subjects,
            href: appRoutes.subjects,
            help: 'People, projects, places, concepts, and other named things in memory.'
          },
          {
            label: 'Themes',
            value: overview.totals.themes,
            href: appRoutes.themes,
            help: 'Groups of related subjects and findings discovered from the graph.'
          }
        ]
      : []
  );

  onMount(() => {
    const updates = new ProcessingEvents(
      (report) => (liveProcessing = report),
      (status) => (processingConnection = status)
    );
    const visibility = () =>
      document.visibilityState === 'visible' ? updates.start() : updates.stop();
    visibility();
    document.addEventListener('visibilitychange', visibility);
    return () => {
      document.removeEventListener('visibilitychange', visibility);
      updates.stop();
    };
  });
</script>

<PageHeader
  title="Dashboard"
  description="Your memory at a glance, with processing progress and recent activity."
/>

{#if !overview || !processing || !usage}
  <Card.Root class="mb-8">
    <Card.Header>
      <Card.Title>Some dashboard data is unavailable</Card.Title>
      <Card.Description>
        Available sections remain visible while the AIZK API recovers.
      </Card.Description>
    </Card.Header>
  </Card.Root>
{/if}

{#if overview}
  <section aria-labelledby="knowledge-heading" class="mb-8">
    <div class="mb-3 flex items-center gap-2">
      <h2
        id="knowledge-heading"
        class="text-muted-foreground text-sm font-medium tracking-wide uppercase"
      >
        Knowledge
      </h2>
      <InfoTip
        label="How to read knowledge totals"
        text="These counts include private memory, organizations you belong to, and any public organizations visible to your account. Select a card to inspect the underlying data."
      />
    </div>
    <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      {#each metrics as metric (metric.label)}
        <a
          href={metric.href}
          class="focus-visible:ring-ring rounded-lg focus-visible:ring-2 focus-visible:outline-none"
        >
          <Card.Root class="hover:bg-muted/30 h-full gap-2 py-4 transition-colors">
            <Card.Header class="px-4">
              <div class="flex items-center gap-2">
                <Card.Description>{metric.label}</Card.Description>
                <InfoTip label={`What ${metric.label} means`} text={metric.help} />
              </div>
              <Card.Title class="text-3xl">{metric.value.toLocaleString('en-US')}</Card.Title>
              <p class="text-muted-foreground text-xs leading-relaxed">{metric.help}</p>
            </Card.Header>
          </Card.Root>
        </a>
      {/each}
    </div>
  </section>
{/if}

{#if processing}
  <section aria-labelledby="processing-heading" class="mb-8">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title id="processing-heading">Processing progress</Card.Title>
          <InfoTip
            label="How processing ETA works"
            text="ETA uses the visible backlog and the recent six-hour and daily completion rates. The progress bars compare work completed in the last 24 hours with the backlog visible now. New ingestion can move the bars backward, so the ETA range is more meaningful than the percentage alone."
          />
        </div>
        <Card.Description>
          {#if processing.state === 'idle'}
            Everything visible is processed.
          {:else if processing.state === 'delayed'}
            Work is waiting, but recent history is not enough for a reliable ETA.
          {:else}
            Full graph enrichment is likely in {formatEtaRange(
              processing.enriched_lower_seconds,
              processing.enriched_upper_seconds
            )}.
          {/if}
        </Card.Description>
        <Card.Action>
          <div class="flex items-center gap-3">
            <span class="text-muted-foreground text-xs" aria-live="polite">
              {processingConnection === 'live'
                ? 'Live'
                : processingConnection === 'paused'
                  ? 'Paused'
                  : 'Reconnecting'}
            </span>
            <a href={appRoutes.processing} class="text-primary text-sm font-medium hover:underline"
              >Open processing</a
            >
          </div>
        </Card.Action>
      </Card.Header>
      <Card.Content class="space-y-7">
        {#if conversion}
          <StageProgress
            stage={conversion}
            label="Source conversion"
            description="Conversion scans and normalizes preserved originals. A converted source can be recalled before every graph finding and theme has finished building."
          />
        {/if}
        {#if projection}
          <StageProgress
            stage={projection}
            label="Graph enrichment"
            description="Graph enrichment extracts findings, links subjects, refreshes profiles, and feeds themes from each source section."
          />
        {/if}
      </Card.Content>
    </Card.Root>
  </section>
{/if}

{#if overview || usage}
  <div class="grid gap-6 xl:grid-cols-3">
    {#if overview}
      <section aria-label="Recent documents">
        <Card.Root class="h-full">
          <Card.Header>
            <div class="flex items-center gap-2">
              <Card.Title>Recent documents</Card.Title>
              <InfoTip
                label="What recent documents shows"
                text="The newest text remembered directly through your private and organization scopes. Preserved files are listed separately."
              />
            </div>
            <Card.Description>Recently remembered text without a preserved file.</Card.Description>
            <Card.Action>
              <a
                href={appHref(appRoutes.sources, { origin: 'document' })}
                class="text-primary text-sm font-medium hover:underline">Browse all</a
              >
            </Card.Action>
          </Card.Header>
          <Card.Content>
            {#if overview.recent_documents.length === 0}
              <p class="text-muted-foreground text-sm">No authored documents are visible yet.</p>
            {:else}
              <ul class="divide-border divide-y">
                {#each overview.recent_documents as document, index (index)}
                  {@const href = webHref(document.source_uri)}
                  <li class="flex flex-wrap items-center gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0">
                    <div class="min-w-0 flex-1">
                      {#if href}
                        <a
                          {href}
                          target="_blank"
                          rel="noreferrer"
                          class="hover:text-primary block truncate text-sm font-medium underline-offset-4 hover:underline"
                        >
                          {document.title}
                        </a>
                      {:else}
                        <p class="truncate text-sm font-medium">{document.title}</p>
                      {/if}
                      <p class="text-muted-foreground text-xs">
                        {document.kind} · {document.date}
                      </p>
                    </div>
                    <ScopeBadges scopes={document.scopes} />
                  </li>
                {/each}
              </ul>
            {/if}
          </Card.Content>
        </Card.Root>
      </section>

      <section aria-label="Recent files">
        <Card.Root class="h-full">
          <Card.Header>
            <div class="flex items-center gap-2">
              <Card.Title>Recent files</Card.Title>
              <InfoTip
                label="What recent files shows"
                text="Uploaded or fetched originals kept by AIZK. Their status shows whether conversion made the contents recallable."
              />
            </div>
            <Card.Description>Preserved originals and their conversion status.</Card.Description>
            <Card.Action>
              <a
                href={appHref(appRoutes.sources, { origin: 'file' })}
                class="text-primary text-sm font-medium hover:underline">Browse all</a
              >
            </Card.Action>
          </Card.Header>
          <Card.Content>
            {#if overview.artifacts.length === 0}
              <p class="text-muted-foreground text-sm">No preserved files are visible yet.</p>
            {:else}
              <ul class="divide-border divide-y">
                {#each overview.artifacts as file, index (index)}
                  {@const href = webHref(file.source_uri)}
                  <li class="flex flex-wrap items-center gap-x-3 gap-y-2 py-3 first:pt-0 last:pb-0">
                    <div class="min-w-0 flex-1">
                      {#if href}
                        <a
                          {href}
                          target="_blank"
                          rel="noreferrer"
                          class="hover:text-primary block truncate text-sm font-medium hover:underline"
                          >{file.name}</a
                        >
                      {:else}
                        <p class="truncate text-sm font-medium">{file.name}</p>
                      {/if}
                      <p class="text-muted-foreground mt-1 text-xs">{file.date} · {file.detail}</p>
                    </div>
                    <Badge variant={file.status === 'failed' ? 'destructive' : 'secondary'}>
                      {file.status}
                    </Badge>
                    <ScopeBadges scopes={file.scopes} />
                  </li>
                {/each}
              </ul>
            {/if}
          </Card.Content>
        </Card.Root>
      </section>
    {/if}

    {#if usage}
      <section aria-label="Usage summary">
        <Card.Root class="h-full">
          <Card.Header>
            <div class="flex items-center gap-2">
              <Card.Title>Usage in 30 days</Card.Title>
              <InfoTip
                label="What usage counts"
                text="Usage counts successful memory operations. Page views and failed requests are excluded. Remember and Share activity can come from connected clients."
              />
            </div>
            <Card.Description
              >Successful operations recorded durably in PostgreSQL.</Card.Description
            >
            <Card.Action>
              <a href={appRoutes.usage} class="text-primary text-sm font-medium hover:underline"
                >View details</a
              >
            </Card.Action>
          </Card.Header>
          <Card.Content>
            <dl class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-muted-foreground text-xs">Requests</dt>
                <dd class="mt-1 text-2xl">{usage.summary.requests.toLocaleString('en-US')}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">Evidence items</dt>
                <dd class="mt-1 text-2xl">{usage.summary.items.toLocaleString('en-US')}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">Recalls</dt>
                <dd class="mt-1 text-lg">{usage.summary.recalls.toLocaleString('en-US')}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground text-xs">Remembers</dt>
                <dd class="mt-1 text-lg">{usage.summary.remembers.toLocaleString('en-US')}</dd>
              </div>
            </dl>
          </Card.Content>
        </Card.Root>
      </section>
    {/if}
  </div>
{/if}
