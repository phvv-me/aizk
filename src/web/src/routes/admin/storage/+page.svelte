<script lang="ts">
  import { formatBytes } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const compressionRatio = $derived(
    data.health && data.health.storage.original_bytes > 0
      ? Math.round(
          (1 - data.health.storage.stored_bytes / data.health.storage.original_bytes) * 100
        )
      : null
  );
</script>

<PageHeader
  title="Storage"
  description="Object store totals, compression savings, and integrity check results."
/>

{#if !data.health}
  <Card.Root>
    <Card.Header>
      <Card.Title>Storage health is unavailable</Card.Title>
      <Card.Description>This will return once the AIZK API answers again.</Card.Description>
    </Card.Header>
  </Card.Root>
{:else}
  <div class="mb-8 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Originals</Card.Description>
        <Card.Title class="text-3xl"
          >{data.health.storage.originals.toLocaleString('en-US')}</Card.Title
        >
        <p class="text-muted-foreground mt-1 text-xs">
          {formatBytes(data.health.storage.logical_bytes)} logical
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Stored</Card.Description>
          <InfoTip
            label="Logical vs physical"
            text="Physical blobs are deduplicated content-addressed objects. Several logical originals can share one physical blob, which is why stored bytes can be smaller than logical bytes even before compression."
          />
        </div>
        <Card.Title class="text-3xl">{formatBytes(data.health.storage.stored_bytes)}</Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          {data.health.storage.physical_blobs.toLocaleString('en-US')} physical blobs
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>Compression saved</Card.Description>
        <Card.Title class="text-3xl"
          >{formatBytes(data.health.storage.compression_saved_bytes)}</Card.Title
        >
        {#if compressionRatio !== null}
          <p class="text-muted-foreground mt-1 text-xs">
            {compressionRatio}% smaller than original
          </p>
        {/if}
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Integrity</Card.Description>
          <InfoTip
            label="What integrity checking covers"
            text="Each object is restored and matched against its content hash by `aizk admin storage compact`, which doubles as this check."
          />
        </div>
        <Card.Title class="flex items-center gap-2 text-lg">
          <Badge
            variant={data.health.storage.failed_integrity_blobs > 0 ? 'destructive' : 'secondary'}
          >
            {data.health.storage.failed_integrity_blobs} failed
          </Badge>
          <Badge variant="outline">{data.health.storage.unverified_blobs} unverified</Badge>
        </Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          Last checked {formatDateTime(data.health.storage.last_integrity_check)}
        </p>
      </Card.Header>
    </Card.Root>
  </div>

  <Card.Root>
    <Card.Header>
      <Card.Title>Storage by scope</Card.Title>
      <Card.Description
        >Logical original-file references in each private or shared scope set.</Card.Description
      >
    </Card.Header>
    <Card.Content>
      {#if data.health.scope_storage.length === 0}
        <p class="text-muted-foreground text-sm">No stored originals yet.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[520px] text-left text-sm">
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Scopes</th>
                <th class="pb-3 font-medium">Revisions</th>
                <th class="pb-3 font-medium">Logical bytes</th>
              </tr>
            </thead>
            <tbody>
              {#each data.health.scope_storage as row (row.scopes.join(','))}
                <tr class="border-b last:border-0">
                  <td class="max-w-sm truncate py-3 pr-4 font-mono text-xs"
                    >{row.scopes.join(', ')}</td
                  >
                  <td class="py-3 pr-4">{row.artifact_revisions.toLocaleString('en-US')}</td>
                  <td class="py-3">{formatBytes(row.logical_bytes)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
