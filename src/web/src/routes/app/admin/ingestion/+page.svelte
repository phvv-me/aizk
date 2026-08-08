<script lang="ts">
  import type { BadgeVariant } from '$lib/components/ui/badge';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MeasuredAt from '$lib/components/MeasuredAt.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ReadingUnavailable from '$lib/components/ReadingUnavailable.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { ConversionState } from '$lib/api';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const doctor = $derived(data.doctor.value);

  const stateLabels: Record<ConversionState, string> = {
    failed: 'Failed',
    unreadable: 'Unreadable',
    active_terminal_queue: 'Active, queue failed',
    active_queued: 'Queued',
    active_stale: 'Stale',
    active_fresh: 'Active'
  };

  function stateVariant(state: ConversionState): BadgeVariant {
    if (state === 'failed' || state === 'active_terminal_queue' || state === 'active_stale') {
      return 'destructive';
    }
    if (state === 'unreadable') return 'outline';
    return 'secondary';
  }
</script>

<PageHeader
  title="Ingestion"
  description="Artifact conversion states, including what was permanently rejected and why."
/>

{#if !doctor}
  <ReadingUnavailable title="Ingestion diagnosis" unreachable={data.doctor.unreachable} />
{:else}
  <MeasuredAt measuredAt={doctor.measured_at} stale={doctor.stale} />
  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Failed</Card.Description>
        <Card.Title class="text-3xl">{doctor.summary.failed_conversions}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Unreadable</Card.Description>
          <InfoTip
            label="What unreadable means"
            text="Docling's own policy check permanently refused this format. This is terminal and excluded from retry, not a bug to fix."
          />
        </div>
        <Card.Title class="text-3xl">{doctor.summary.unreadable_conversions}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Active</Card.Description>
        <Card.Title class="text-3xl"
          >{doctor.summary.queued_active_conversions +
            doctor.summary.fresh_active_conversions +
            doctor.summary.stale_active_conversions +
            doctor.summary.orphaned_active_conversions}</Card.Title
        >
      </Card.Header>
    </Card.Root>
  </div>

  <Card.Root>
    <Card.Header>
      <Card.Title>Conversions</Card.Title>
      <Card.Description>
        Failed, unreadable, and active originals, with their stored error and next safe action.
      </Card.Description>
    </Card.Header>
    <Card.Content>
      {#if doctor.conversions.length === 0}
        <p class="text-muted-foreground text-sm">
          Nothing failed, unreadable, or active right now.
        </p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[760px] text-left text-sm">
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Artifact</th>
                <th class="pb-3 font-medium">State</th>
                <th class="pb-3 font-medium">Updated</th>
                <th class="pb-3 font-medium">What was rejected and why</th>
              </tr>
            </thead>
            <tbody>
              {#each doctor.conversions as conversion (conversion.content_id)}
                <tr class="border-b last:border-0">
                  <td class="max-w-xs truncate py-3 pr-4 font-medium">{conversion.artifact_name}</td
                  >
                  <td class="py-3 pr-4">
                    <Badge variant={stateVariant(conversion.state)}
                      >{stateLabels[conversion.state]}</Badge
                    >
                  </td>
                  <td class="text-muted-foreground py-3 pr-4"
                    >{formatDateTime(conversion.updated_at)}</td
                  >
                  <td class="py-3">
                    {#if conversion.error}
                      <p class="text-sm">{conversion.error.type}</p>
                      {#if conversion.error.sanitized_message}
                        <p class="text-muted-foreground mt-0.5 text-xs">
                          {conversion.error.sanitized_message}
                        </p>
                      {/if}
                    {:else}
                      <p class="text-muted-foreground text-xs">{conversion.retry_guidance}</p>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
