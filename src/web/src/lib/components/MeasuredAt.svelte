<script lang="ts">
  import InfoTip from '$lib/components/InfoTip.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { formatAge, formatDateTime } from '$lib/format';

  let { measuredAt, stale }: { measuredAt: string; stale: boolean } = $props();

  const age = $derived(formatAge(measuredAt));
</script>

<div class="text-muted-foreground mb-6 flex flex-wrap items-center gap-2 text-sm">
  <span>Measured {age}, at {formatDateTime(measuredAt)}.</span>
  {#if stale}
    <Badge variant="destructive">Stale</Badge>
  {/if}
  <InfoTip
    label="Why this is a measurement rather than a live reading"
    text="The probes behind this page count rows and read policies across every scope, which only the database owner may do, and the internet-facing process serving this console is deliberately denied that credential. A worker holding it measures on a schedule and leaves the reading here. Stale means the last pass is older than the schedule allows, so the worker is behind or stopped."
  />
</div>
