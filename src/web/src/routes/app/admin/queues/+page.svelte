<script lang="ts">
  import type { BadgeVariant } from '$lib/components/ui/badge';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MeasuredAt from '$lib/components/MeasuredAt.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ReadingUnavailable from '$lib/components/ReadingUnavailable.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { Severity } from '$lib/api';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const doctor = $derived(data.doctor.value);

  function severityVariant(severity: Severity): BadgeVariant {
    if (severity === 'error') return 'destructive';
    if (severity === 'warning') return 'outline';
    return 'secondary';
  }
</script>

<PageHeader
  title="Queues"
  description="Pending, running, and failed background work, grouped by entrypoint."
/>

{#if !doctor}
  <ReadingUnavailable title="Queue diagnosis" unreachable={data.doctor.unreachable} />
{:else}
  <MeasuredAt measuredAt={doctor.measured_at} stale={doctor.stale} />
  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Currently failed</Card.Description>
        <Card.Title class="text-3xl">{doctor.summary.current_failed_jobs}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Stale picked</Card.Description>
        <Card.Title class="text-3xl">{doctor.summary.stale_picked_jobs}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Long running</Card.Description>
        <Card.Title class="text-3xl">{doctor.summary.long_running_picked_jobs}</Card.Title>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Recent exceptions</Card.Description>
          <InfoTip
            label="What recent exceptions means"
            text={`Failure events over the last ${doctor.history_seconds / 3600} hours, including ones already recovered by a retry.`}
          />
        </div>
        <Card.Title class="text-3xl">{doctor.summary.recent_exception_events}</Card.Title>
      </Card.Header>
    </Card.Root>
  </div>

  {#if doctor.findings.length > 0}
    <Card.Root class="mb-8">
      <Card.Header>
        <Card.Title>Findings</Card.Title>
        <Card.Description>Concise conclusions and the next safe action for each.</Card.Description>
      </Card.Header>
      <Card.Content>
        <ul class="divide-border divide-y">
          {#each doctor.findings as finding (finding.code)}
            <li class="flex flex-wrap items-start justify-between gap-2 py-3 first:pt-0">
              <div class="min-w-0">
                <p class="text-sm font-medium">{finding.message}</p>
                <p class="text-muted-foreground mt-0.5 text-xs">{finding.action}</p>
              </div>
              <Badge variant={severityVariant(finding.severity)}>{finding.count}</Badge>
            </li>
          {/each}
        </ul>
      </Card.Content>
    </Card.Root>
  {/if}

  <Card.Root class="mb-8">
    <Card.Header>
      <Card.Title>Failures by entrypoint</Card.Title>
      <Card.Description
        >Currently retained failures, grouped without exception text.</Card.Description
      >
    </Card.Header>
    <Card.Content>
      {#if doctor.queue_failure_groups.length === 0}
        <p class="text-muted-foreground text-sm">No retained failures.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Entrypoint</th>
                <th class="pb-3 font-medium">Error</th>
                <th class="pb-3 font-medium">Count</th>
                <th class="pb-3 font-medium">Attempts</th>
                <th class="pb-3 font-medium">Oldest</th>
                <th class="pb-3 font-medium">Remediation</th>
              </tr>
            </thead>
            <tbody>
              {#each doctor.queue_failure_groups as group (group.entrypoint + (group.error?.fingerprint ?? ''))}
                <tr class="border-b last:border-0">
                  <td class="py-3 pr-4 font-medium">{group.entrypoint}</td>
                  <td class="py-3 pr-4">
                    {#if group.error}
                      <span title={group.error.sanitized_message ?? undefined}
                        >{group.error.type}</span
                      >
                    {:else}
                      N/A
                    {/if}
                  </td>
                  <td class="py-3 pr-4">{group.count}</td>
                  <td class="py-3 pr-4">{group.attempts_min}–{group.attempts_max}</td>
                  <td class="text-muted-foreground py-3 pr-4">{formatDateTime(group.oldest_at)}</td>
                  <td class="text-muted-foreground py-3">{group.remediation}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>

  <Card.Root>
    <Card.Header>
      <Card.Title>Unhealthy leases</Card.Title>
      <Card.Description>Picked jobs that stopped heartbeating or ran long.</Card.Description>
    </Card.Header>
    <Card.Content>
      {#if doctor.queue_issues.length === 0}
        <p class="text-muted-foreground text-sm">No stale or long-running leases.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Entrypoint</th>
                <th class="pb-3 font-medium">Kind</th>
                <th class="pb-3 font-medium">Age</th>
                <th class="pb-3 font-medium">Attempts</th>
                <th class="pb-3 font-medium">Guidance</th>
              </tr>
            </thead>
            <tbody>
              {#each doctor.queue_issues as issue (issue.id)}
                <tr class="border-b last:border-0">
                  <td class="py-3 pr-4 font-medium">{issue.entrypoint}</td>
                  <td class="py-3 pr-4">
                    <Badge variant="outline"
                      >{issue.kind === 'stale_picked' ? 'Stale' : 'Long running'}</Badge
                    >
                  </td>
                  <td class="text-muted-foreground py-3 pr-4"
                    >{Math.round(issue.age_seconds / 60)} min</td
                  >
                  <td class="py-3 pr-4">{issue.attempts}</td>
                  <td class="text-muted-foreground py-3">{issue.retry_guidance}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
