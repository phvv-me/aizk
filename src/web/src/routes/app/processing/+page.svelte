<script lang="ts">
  import { invalidateAll } from '$app/navigation';
  import { onMount } from 'svelte';
  import { RefreshCw } from '@lucide/svelte';
  import type { ArtifactView, ProcessingReport } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import StageProgress from '$lib/components/StageProgress.svelte';
  import { Badge, type BadgeVariant } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime, formatEtaRange } from '$lib/format';
  import { ProcessingEvents, type ProcessingConnection } from '$lib/processing-events';
  import { cn, webHref } from '$lib/utils';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();
  let refreshing = $state(false);
  let liveProcessing = $state<ProcessingReport | null>(null);
  let processingConnection = $state<ProcessingConnection>('connecting');
  const processing = $derived(liveProcessing ?? data.processing);

  const statusVariants: Record<ArtifactView['status'], BadgeVariant> = {
    queued: 'outline',
    processing: 'secondary',
    ready: 'default',
    failed: 'destructive'
  };
  const conversion = $derived(processing?.stages.find((stage) => stage.key === 'conversion'));
  const projection = $derived(processing?.stages.find((stage) => stage.key === 'graph_projection'));

  async function refresh() {
    refreshing = true;
    await invalidateAll();
    refreshing = false;
  }

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
  title="Processing"
  description="Understand what is waiting, how quickly work is draining, and when bulk ingestion should finish."
/>

{#if !processing}
  <Card.Root>
    <Card.Header>
      <Card.Title>Processing unavailable</Card.Title>
      <Card.Description
        >Queue progress will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else}
  <Card.Root class="mb-6">
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>
          {#if processing.state === 'idle'}
            Everything visible is processed
          {:else if processing.state === 'delayed'}
            Processing needs attention
          {:else}
            Processing is active
          {/if}
        </Card.Title>
        <InfoTip
          label="How processing status is decided"
          text="Status is derived only from source and source-section rows visible to you. Active means the current one-hour or six-hour completion rate can estimate the backlog. Delayed means recent completions are too sparse or failed work needs attention."
        />
      </div>
      <Card.Description>
        Recallable ETA {formatEtaRange(
          processing.recallable_lower_seconds,
          processing.recallable_upper_seconds
        )}. Full enrichment ETA {formatEtaRange(
          processing.enriched_lower_seconds,
          processing.enriched_upper_seconds
        )}.
      </Card.Description>
      <Card.Action>
        <div class="flex items-center gap-2">
          <span class="text-muted-foreground text-xs" aria-live="polite">
            {processingConnection === 'live'
              ? 'Live updates'
              : processingConnection === 'paused'
                ? 'Updates paused'
                : 'Reconnecting'}
          </span>
          <Button variant="ghost" size="sm" onclick={refresh} disabled={refreshing}>
            <RefreshCw class={cn(refreshing && 'animate-spin')} aria-hidden="true" />
            Refresh
          </Button>
        </div>
      </Card.Action>
    </Card.Header>
  </Card.Root>

  <Card.Root class="mb-6">
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Stage progress</Card.Title>
        <InfoTip
          label="Why there are two stages"
          text="Conversion makes preserved originals searchable. Graph enrichment then extracts findings, links subjects, refreshes profiles, and contributes to themes. Pending conversions create source sections later, so full enrichment ETA stays unavailable until conversion clears and the complete downstream workload is known."
        />
      </div>
      <Card.Description>
        Progress compares work completed in the last 24 hours with waiting, active, and failed work.
        ETA uses current throughput and bounded uncertainty instead of treating yesterday's pace as
        a possible current pace.
      </Card.Description>
    </Card.Header>
    <Card.Content class="space-y-8">
      {#if conversion}
        <StageProgress
          stage={conversion}
          label="Source conversion"
          description="Waiting includes accepted originals that have not reached conversion. Active conversion normalizes and indexes the source. Failed originals are excluded from ETA until they are repaired or retried."
        />
      {/if}
      {#if projection}
        <StageProgress
          stage={projection}
          label="Graph enrichment"
          description="Waiting counts source sections whose findings and subject relationships have not finished projecting. Running and failed sub-states are not yet safely attributable per user, so they are shown as not tracked. ETA requires enough current completion history to avoid stale or extreme ranges."
        />
      {/if}
    </Card.Content>
  </Card.Root>

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Recent originals</Card.Title>
        <InfoTip
          label="How to read original states"
          text="Queued waits for secure processing. Processing is converting and indexing. Ready can be recalled. Failed is a terminal source state and may need operator attention."
        />
      </div>
      <Card.Description>
        Live updates run while this page is in the foreground and reconnect automatically.
      </Card.Description>
    </Card.Header>
    <Card.Content>
      {#if processing.recent.length === 0}
        <p class="text-muted-foreground text-sm">No preserved originals are visible.</p>
      {:else}
        <ul class="divide-border divide-y">
          {#each processing.recent as artifact, index (index)}
            {@const href = webHref(artifact.source_uri)}
            <li class="flex flex-wrap items-center gap-x-4 gap-y-2 py-3 first:pt-0 last:pb-0">
              <div class="min-w-0 flex-1">
                {#if href}
                  <a {href} target="_blank" rel="noreferrer" class="font-medium hover:underline">
                    {artifact.name}
                  </a>
                {:else}
                  <p class="font-medium">{artifact.name}</p>
                {/if}
                <p class="text-muted-foreground mt-1 text-xs">
                  {artifact.detail} · {artifact.date} · refreshed {formatDateTime(
                    processing.generated_at
                  )}
                </p>
              </div>
              <ScopeBadges scopes={artifact.scopes} />
              <Badge variant={statusVariants[artifact.status]}>{artifact.status}</Badge>
            </li>
          {/each}
        </ul>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
