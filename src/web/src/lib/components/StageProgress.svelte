<script lang="ts">
  import type { StageEstimate } from '$lib/api';
  import { formatEtaRange } from '$lib/format';
  import InfoTip from './InfoTip.svelte';

  let { stage, label, description }: { stage: StageEstimate; label: string; description: string } =
    $props();

  const etaLabel = $derived(
    stage.eta_status === 'blocked'
      ? 'Blocked work needs attention before an ETA is possible'
      : stage.eta_status === 'insufficient_history'
        ? 'ETA unavailable until the current pace is measurable'
        : formatEtaRange(stage.lower_seconds, stage.upper_seconds)
  );
  const statusLabel = $derived(
    stage.eta_status === 'estimating'
      ? `${stage.confidence} confidence`
      : stage.eta_status.replaceAll('_', ' ')
  );
</script>

<section class="space-y-3" aria-label={label}>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <div class="flex items-center gap-2">
        <h3 class="font-medium">{label}</h3>
        <InfoTip label={`How to read ${label}`} text={description} />
      </div>
      <p class="text-muted-foreground mt-1 text-sm">
        {etaLabel}
      </p>
    </div>
    <span class="text-muted-foreground text-xs font-medium uppercase">{statusLabel}</span>
  </div>
  <div
    class="bg-muted h-3 overflow-hidden rounded-full"
    role="progressbar"
    aria-label={`${label} recent workload progress`}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={stage.progress_percent}
  >
    <div
      class="bg-primary h-full min-w-0 rounded-full transition-[width] motion-reduce:transition-none"
      style={`width: ${stage.progress_percent}%`}
    ></div>
  </div>
  <div class="text-muted-foreground grid grid-cols-2 gap-2 text-xs sm:grid-cols-3 xl:grid-cols-6">
    <span
      ><strong class="text-foreground">{stage.progress_percent}%</strong> recent workload cleared</span
    >
    <span
      ><strong class="text-foreground">{stage.queued.toLocaleString('en-US')}</strong> waiting</span
    >
    <span
      ><strong class="text-foreground"
        >{stage.running?.toLocaleString('en-US') ?? 'Not tracked'}</strong
      >
      active</span
    >
    <span
      ><strong class="text-foreground"
        >{stage.failed?.toLocaleString('en-US') ?? 'Not tracked'}</strong
      >
      failed</span
    >
    <span
      ><strong class="text-foreground">{stage.completed_24h.toLocaleString('en-US')}</strong>
      completed in 24 hours</span
    >
    <span
      ><strong class="text-foreground">{stage.throughput_per_hour.toFixed(1)}</strong> per hour
      {#if stage.throughput_window_hours}
        over the last {stage.throughput_window_hours}
        {stage.throughput_window_hours === 1 ? 'hour' : 'hours'}
      {:else}
        currently
      {/if}</span
    >
  </div>
</section>
