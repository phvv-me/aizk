<script lang="ts">
  import type { Operation } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Input } from '$lib/components/ui/input';
  import { Label } from '$lib/components/ui/label';
  import { formatOperation } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const operations: Operation[] = [
    'recall',
    'remember_text',
    'remember_file',
    'share',
    'artifact_read',
    'web_search',
    'web_fetch'
  ];

  const selectClass =
    'border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-9 rounded-md border bg-transparent px-2 text-sm outline-none focus-visible:ring-[3px]';
</script>

<PageHeader
  title="Usage"
  description="Durable successful operations, filterable by kind, caller, organization, and window."
/>

<Card.Root class="mb-6">
  <Card.Header>
    <Card.Title>Filters</Card.Title>
    <Card.Description
      >Every filter composes; leave a field blank to leave it unfiltered.</Card.Description
    >
  </Card.Header>
  <Card.Content>
    <form method="GET" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      <div class="space-y-2">
        <div class="flex items-center gap-1.5">
          <Label for="usage-operation">Kind</Label>
          <InfoTip
            label="Why Recall is named that"
            text="Recorded as recall, the durable name for what the find tool does. It is not a second operation."
          />
        </div>
        <select
          id="usage-operation"
          name="operation"
          class={selectClass}
          value={data.filter.operation ?? ''}
        >
          <option value="">All kinds</option>
          {#each operations as operation (operation)}
            <option value={operation}>{formatOperation(operation)}</option>
          {/each}
        </select>
      </div>
      <div class="space-y-2">
        <Label for="usage-actor">Caller ID</Label>
        <Input
          id="usage-actor"
          name="actor_id"
          value={data.filter.actorId ?? ''}
          placeholder="uuid"
        />
      </div>
      <div class="space-y-2">
        <Label for="usage-scope">Organization ID</Label>
        <Input
          id="usage-scope"
          name="scope_id"
          value={data.filter.scopeId ?? ''}
          placeholder="uuid"
        />
      </div>
      <div class="space-y-2">
        <Label for="usage-start">From</Label>
        <Input
          id="usage-start"
          type="datetime-local"
          name="start"
          value={data.filter.start ?? ''}
        />
      </div>
      <div class="space-y-2">
        <Label for="usage-end">Until</Label>
        <Input id="usage-end" type="datetime-local" name="end" value={data.filter.end ?? ''} />
      </div>
      <div class="sm:col-span-2 lg:col-span-5">
        <Button type="submit" variant="secondary">Apply filters</Button>
      </div>
    </form>
  </Card.Content>
</Card.Root>

{#if !data.usage}
  <Card.Root>
    <Card.Header>
      <Card.Title>Usage is unavailable</Card.Title>
      <Card.Description>This will return once the AIZK API answers again.</Card.Description>
    </Card.Header>
  </Card.Root>
{:else}
  <div class="grid gap-6 xl:grid-cols-2">
    <Card.Root>
      <Card.Header>
        <Card.Title>By caller</Card.Title>
        <Card.Description>Aggregated under the filters above.</Card.Description>
      </Card.Header>
      <Card.Content>
        {#if data.usage.by_actor.length === 0}
          <p class="text-muted-foreground text-sm">No usage matches this filter.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full min-w-[520px] text-left text-sm">
              <thead>
                <tr class="border-b">
                  <th class="pb-3 font-medium">Caller</th>
                  <th class="pb-3 font-medium">Recalls</th>
                  <th class="pb-3 font-medium">Remembers</th>
                  <th class="pb-3 font-medium">Shares</th>
                  <th class="pb-3 font-medium">Reads</th>
                </tr>
              </thead>
              <tbody>
                {#each data.usage.by_actor as row (row.actor_id)}
                  <tr class="border-b last:border-0">
                    <td class="max-w-[10rem] truncate py-3 pr-4 font-mono text-xs"
                      >{row.actor_id}</td
                    >
                    <td class="py-3 pr-4">{row.recalls.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.remembers.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.shares.toLocaleString('en-US')}</td>
                    <td class="py-3">{row.artifact_reads.toLocaleString('en-US')}</td>
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
        <Card.Title>By organization</Card.Title>
        <Card.Description>The private or shared scope each operation touched.</Card.Description>
      </Card.Header>
      <Card.Content>
        {#if data.usage.by_scope.length === 0}
          <p class="text-muted-foreground text-sm">No usage matches this filter.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full min-w-[520px] text-left text-sm">
              <thead>
                <tr class="border-b">
                  <th class="pb-3 font-medium">Scope</th>
                  <th class="pb-3 font-medium">Recalls</th>
                  <th class="pb-3 font-medium">Remembers</th>
                  <th class="pb-3 font-medium">Shares</th>
                  <th class="pb-3 font-medium">Reads</th>
                </tr>
              </thead>
              <tbody>
                {#each data.usage.by_scope as row (row.scope_id)}
                  <tr class="border-b last:border-0">
                    <td class="max-w-[10rem] truncate py-3 pr-4 font-mono text-xs"
                      >{row.scope_id}</td
                    >
                    <td class="py-3 pr-4">{row.recalls.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.remembers.toLocaleString('en-US')}</td>
                    <td class="py-3 pr-4">{row.shares.toLocaleString('en-US')}</td>
                    <td class="py-3">{row.artifact_reads.toLocaleString('en-US')}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </Card.Content>
    </Card.Root>
  </div>
{/if}
