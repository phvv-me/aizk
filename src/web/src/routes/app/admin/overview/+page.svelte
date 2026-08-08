<script lang="ts">
  import { enhance } from '$app/forms';
  import { MessageCircleQuestion } from '@lucide/svelte';
  import { formatBytes } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MeasuredAt from '$lib/components/MeasuredAt.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ReadingUnavailable from '$lib/components/ReadingUnavailable.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { feedback } from '$lib/forms';
  import { formatDateTime } from '$lib/format';
  import { adminRoutes } from '$lib/routes';
  import type { ActionData, PageServerData } from './$types';

  let { data, form }: { data: PageServerData; form: ActionData } = $props();
  let probing = $state(false);

  const health = $derived(data.health.value);
  const hardware = $derived(data.hardware.value);

  const percent = (used: number | null, total: number | null): number | null =>
    used === null || total === null || total === 0 ? null : Math.round((used / total) * 100);

  const memoryUsedPct = $derived(
    hardware
      ? percent(
          hardware.memory_total_bytes !== null && hardware.memory_available_bytes !== null
            ? hardware.memory_total_bytes - hardware.memory_available_bytes
            : null,
          hardware.memory_total_bytes
        )
      : null
  );
  const diskUsedPct = $derived(
    hardware
      ? percent(
          hardware.disk_total_bytes !== null && hardware.disk_available_bytes !== null
            ? hardware.disk_total_bytes - hardware.disk_available_bytes
            : null,
          hardware.disk_total_bytes
        )
      : null
  );
</script>

<PageHeader
  title="Overview"
  description="Schema, security, storage, queue, and serving health at a glance."
/>

{#if !health}
  <ReadingUnavailable title="Health" unreachable={data.health.unreachable} />
{:else}
  <MeasuredAt measuredAt={health.measured_at} stale={health.stale} />
  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>
          Migration
          <InfoTip
            label="What migration state means"
            text="Which schema revision this database is stamped at, and the newest revision the code ships. At head means the two agree, so nothing is pending."
          />
        </Card.Description>
        <Card.Title class="flex items-center gap-2 text-lg">
          <Badge variant={health.migration.up_to_date ? 'secondary' : 'destructive'}>
            {health.migration.up_to_date ? 'At head' : 'Behind head'}
          </Badge>
        </Card.Title>
        <!-- An arrow between two identical revisions reads as a pending move that is not
             pending, so a schema at head names the one revision it is on. -->
        <p class="text-muted-foreground mt-1 text-xs">
          {#if health.migration.up_to_date}
            {health.migration.head}
          {:else}
            {health.migration.current ?? 'unstamped'} &rarr; {health.migration.head}
          {/if}
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>
          RLS violations
          <InfoTip
            label="What an RLS violation is"
            text="Not rows that leaked. This reads the catalog and counts scoped tables whose schema would permit a leak, by not enabling row security, not forcing it, or carrying no policy. Zero is the healthy state and is how a table shipped unprotected gets caught."
          />
        </Card.Description>
        <Card.Title class="text-3xl">{health.rls_violations.length}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>
            Queue
            <InfoTip
              label="What queue counts mean"
              text="Pending waits to run, running holds a live worker lease, and failed is retained for repair rather than dropped, so a non-zero count can be an old failure nothing has retried rather than a live incident."
            />
          </Card.Description>
        </div>
        <Card.Title class="text-lg"
          >{health.queue.pending} pending &middot; {health.queue.running} running</Card.Title
        >
        <p class="text-muted-foreground mt-1 text-xs">
          {health.queue.failed} failed &middot;
          <a href={adminRoutes.queues} class="text-primary hover:underline">Open queues</a>
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>
          Stored
          <InfoTip
            label="How stored totals are measured"
            text="Row counts across every scope, which is why they are measured by a process holding platform-wide read rather than by this page."
          />
        </Card.Description>
        <Card.Title class="text-lg">{formatBytes(health.storage.stored_bytes)}</Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          {formatBytes(health.storage.compression_saved_bytes)} saved by compression &middot;
          <a href={adminRoutes.storage} class="text-primary hover:underline">Open storage</a>
        </p>
      </Card.Header>
    </Card.Root>
  </div>

  {#if health.rls_violations.length > 0}
    <Card.Root class="border-destructive mb-8">
      <Card.Header>
        <Card.Title>Row security violations</Card.Title>
        <Card.Description>Every scoped table must force row security with no leak.</Card.Description
        >
      </Card.Header>
      <Card.Content>
        <ul class="list-inside list-disc space-y-1 text-sm">
          {#each health.rls_violations as violation (violation)}
            <li>{violation}</li>
          {/each}
        </ul>
      </Card.Content>
    </Card.Root>
  {/if}

  <div class="mb-8 grid gap-6 xl:grid-cols-2">
    <Card.Root>
      <Card.Header>
        <Card.Title>Serving endpoints</Card.Title>
        <Card.Description>
          Reachability and served identity for each model lane.
          <InfoTip
            label="How lanes are probed"
            text="Each lane is probed for the model it actually serves, not the one configured, so a lane answering with the wrong model shows as a mismatch rather than as healthy."
          />
        </Card.Description>
      </Card.Header>
      <Card.Content>
        {#if health.endpoints.length === 0}
          <p class="text-muted-foreground text-sm">No serving endpoints are configured.</p>
        {:else}
          <ul class="divide-border divide-y">
            {#each health.endpoints as endpoint (endpoint.name)}
              <li class="flex flex-wrap items-center justify-between gap-2 py-2.5 first:pt-0">
                <div class="min-w-0">
                  <p class="text-sm font-medium capitalize">{endpoint.name}</p>
                  <p class="text-muted-foreground truncate text-xs">
                    {endpoint.model ?? endpoint.url}
                  </p>
                </div>
                <div class="flex items-center gap-2">
                  {#if endpoint.matched === false}
                    <Badge variant="outline">Model mismatch</Badge>
                  {/if}
                  <Badge variant={endpoint.reachable ? 'secondary' : 'destructive'}>
                    {endpoint.reachable ? 'Reachable' : 'Unreachable'}
                  </Badge>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </Card.Content>
    </Card.Root>

    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>Row counts</Card.Title>
          <InfoTip
            label="What row counts cover"
            text="Owner-side counts of every principal table, not filtered by any caller's row security."
          />
        </div>
      </Card.Header>
      <Card.Content>
        <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
          {#each Object.entries(health.row_counts) as [table, count] (table)}
            <div>
              <dt class="text-muted-foreground truncate text-xs">{table}</dt>
              <dd>{count.toLocaleString('en-US')}</dd>
            </div>
          {/each}
        </dl>
      </Card.Content>
    </Card.Root>
  </div>

  <Card.Root class="mb-8">
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Host and model-lane load</Card.Title>
        <InfoTip
          label="Where this comes from"
          text="Read from VictoriaMetrics, which the optional observability profile already collects. No process here holds direct GPU device access, so per-lane KV-cache occupancy and queue depth are the real GPU-adjacent signal available, not raw device telemetry."
        />
      </div>
    </Card.Header>
    <Card.Content>
      {#if !hardware?.reachable}
        <p class="text-muted-foreground text-sm">
          Hardware metrics are not configured for this deployment.
        </p>
      {:else}
        <div class="mb-5 grid gap-5 sm:grid-cols-3">
          <div>
            <dt class="text-muted-foreground text-xs">Load average</dt>
            <dd class="mt-1 text-lg">
              {hardware.load1?.toFixed(2) ?? 'N/A'} / {hardware.load5?.toFixed(2) ?? 'N/A'} / {hardware.load15?.toFixed(
                2
              ) ?? 'N/A'}
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs">Memory used</dt>
            <dd class="mt-1 text-lg">
              {memoryUsedPct !== null ? `${memoryUsedPct}%` : 'N/A'}
              {#if hardware.memory_total_bytes !== null}
                <span class="text-muted-foreground text-xs"
                  >of {formatBytes(hardware.memory_total_bytes)}</span
                >
              {/if}
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs">Disk used (root)</dt>
            <dd class="mt-1 text-lg">
              {diskUsedPct !== null ? `${diskUsedPct}%` : 'N/A'}
              {#if hardware.disk_total_bytes !== null}
                <span class="text-muted-foreground text-xs"
                  >of {formatBytes(hardware.disk_total_bytes)}</span
                >
              {/if}
            </dd>
          </div>
        </div>
        <ul class="divide-border divide-y">
          {#each hardware.lanes as lane (lane.service)}
            <li class="flex flex-wrap items-center justify-between gap-2 py-2.5 first:pt-0">
              <p class="text-sm font-medium">{lane.service}</p>
              <div class="text-muted-foreground flex items-center gap-3 text-xs">
                {#if lane.up}
                  <span
                    >{lane.kv_cache_usage_pct?.toFixed(0) ?? 'N/A'}% KV cache &middot; {lane.requests_running ??
                      0} running &middot; {lane.requests_waiting ?? 0} waiting</span
                  >
                {/if}
                <Badge variant={lane.up ? 'secondary' : 'outline'}>{lane.up ? 'Up' : 'Down'}</Badge>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </Card.Content>
  </Card.Root>

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Live recall probe</Card.Title>
        <InfoTip
          label="Why this is separate"
          text="A real retrieval over the largest visible corpus, which took roughly 3 seconds in production. It runs only when asked, never on page load."
        />
      </div>
      <Card.Description>
        Confirm end-to-end retrieval against real stored memory.
        <InfoTip
          label="What the recall probe does"
          text="Runs a real recall against the largest stored corpus, so it exercises embedding, retrieval and reranking rather than reporting configuration. It costs a few seconds, which is why it is a button rather than part of the page load."
        />
      </Card.Description>
      <Card.Action>
        <form
          method="POST"
          action="?/recall"
          use:enhance={feedback('Recall probe complete.', {
            reset: false,
            pending: (active) => (probing = active)
          })}
        >
          <Button type="submit" variant="secondary" disabled={probing}>
            <MessageCircleQuestion aria-hidden="true" />
            {probing ? 'Probing…' : 'Run probe'}
          </Button>
        </form>
      </Card.Action>
    </Card.Header>
    {#if form?.recall !== undefined}
      <Card.Content>
        {#if form.recall === null}
          <p class="text-muted-foreground text-sm">No corpus is stored yet to probe.</p>
        {:else}
          <dl class="grid gap-4 sm:grid-cols-3">
            <div>
              <dt class="text-muted-foreground text-xs">Candidates</dt>
              <dd class="mt-1 text-xl">{form.recall.candidates}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Latency</dt>
              <dd class="mt-1 text-xl">{form.recall.latency_ms.toFixed(0)} ms</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs">Top source</dt>
              <dd class="mt-1 truncate text-xl">{form.recall.top_source ?? 'N/A'}</dd>
            </div>
          </dl>
          {#if form.recall.error}
            <p class="text-destructive mt-3 text-sm">{form.recall.error}</p>
          {/if}
        {/if}
      </Card.Content>
    {/if}
  </Card.Root>
{/if}
