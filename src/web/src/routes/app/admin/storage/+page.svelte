<script lang="ts">
  import { formatBytes } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MeasuredAt from '$lib/components/MeasuredAt.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ReadingUnavailable from '$lib/components/ReadingUnavailable.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const health = $derived(data.health.value);
  const storage = $derived(health?.storage ?? null);

  const percentSmaller = (kept: number, original: number): number | null =>
    original > 0 ? Math.round((1 - kept / original) * 100) : null;

  const compressionRatio = $derived(
    storage ? percentSmaller(storage.stored_bytes, storage.original_bytes) : null
  );
  // Several logical originals can point at one content-addressed blob, so what deduplication
  // saved is the gap between what callers referenced and what was physically kept once.
  const deduplicationRatio = $derived(
    storage ? percentSmaller(storage.original_bytes, storage.logical_bytes) : null
  );
  const deduplicatedBytes = $derived(storage ? storage.logical_bytes - storage.original_bytes : 0);
  const verifiedBlobs = $derived(storage ? storage.physical_blobs - storage.unverified_blobs : 0);
  const scopeStorage = $derived(
    health ? [...health.scope_storage].sort((a, b) => b.logical_bytes - a.logical_bytes) : []
  );
  const scopeBytes = $derived(scopeStorage.reduce((total, row) => total + row.logical_bytes, 0));
</script>

<PageHeader
  title="Storage"
  description="What callers referenced, what was physically kept, and whether it still verifies."
/>

{#if !health || !storage}
  <ReadingUnavailable title="Storage health" unreachable={data.health.unreachable} />
{:else}
  <MeasuredAt measuredAt={health.measured_at} stale={health.stale} />

  <div class="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Originals</Card.Description>
          <InfoTip
            label="What an original is"
            text="One stored revision of a file a caller kept. Keeping the same file twice creates two revisions, so this counts references rather than distinct content."
          />
        </div>
        <Card.Title class="text-3xl">{storage.originals.toLocaleString('en-US')}</Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          {formatBytes(storage.logical_bytes)} referenced
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Physical blobs</Card.Description>
          <InfoTip
            label="Logical against physical"
            text="Blobs are content addressed, so identical bytes are kept once no matter how many originals or scopes point at them. This is why physical bytes can be far below referenced bytes before compression is even counted."
          />
        </div>
        <Card.Title class="text-3xl">{storage.physical_blobs.toLocaleString('en-US')}</Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          {formatBytes(storage.original_bytes)} before compression
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <Card.Description>On disk</Card.Description>
        <Card.Title class="text-3xl">{formatBytes(storage.stored_bytes)}</Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          {formatBytes(storage.compression_saved_bytes)} saved by compression{compressionRatio !==
          null
            ? `, ${compressionRatio}% smaller`
            : ''}
        </p>
      </Card.Header>
    </Card.Root>
    <Card.Root class="gap-2 py-4">
      <Card.Header class="px-4">
        <div class="flex items-center gap-2">
          <Card.Description>Integrity</Card.Description>
          <InfoTip
            label="What integrity checking covers"
            text="Each object is restored and matched against its content hash by `aizk admin storage compact`, which doubles as this check. Unverified is not a failure, only an object that check has not reached yet."
          />
        </div>
        <Card.Title class="flex items-center gap-2 text-lg">
          <Badge variant={storage.failed_integrity_blobs > 0 ? 'destructive' : 'secondary'}>
            {storage.failed_integrity_blobs} failed
          </Badge>
          <Badge variant="outline">{storage.unverified_blobs} unverified</Badge>
        </Card.Title>
        <p class="text-muted-foreground mt-1 text-xs">
          Last checked {formatDateTime(storage.last_integrity_check)}
        </p>
      </Card.Header>
    </Card.Root>
  </div>

  <div class="mb-6 grid gap-6 xl:grid-cols-[1fr_1.5fr]">
    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>From referenced to on disk</Card.Title>
          <InfoTip
            label="How to read this"
            text="Two independent savings applied in order. Deduplication removes repeated content across originals and scopes, then compression shrinks what survives. Each line is measured, not estimated."
          />
        </div>
        <Card.Description
          >Every stage between what callers see and what disk holds.</Card.Description
        >
      </Card.Header>
      <Card.Content>
        <dl class="space-y-3 text-sm">
          <div class="flex items-baseline justify-between gap-4">
            <dt class="text-muted-foreground">Referenced by originals</dt>
            <dd class="font-medium">{formatBytes(storage.logical_bytes)}</dd>
          </div>
          <div class="flex items-baseline justify-between gap-4">
            <dt class="text-muted-foreground">
              Saved by deduplication{deduplicationRatio !== null ? ` (${deduplicationRatio}%)` : ''}
            </dt>
            <dd class="font-medium">&minus;{formatBytes(deduplicatedBytes)}</dd>
          </div>
          <div class="flex items-baseline justify-between gap-4">
            <dt class="text-muted-foreground">Distinct content</dt>
            <dd class="font-medium">{formatBytes(storage.original_bytes)}</dd>
          </div>
          <div class="flex items-baseline justify-between gap-4">
            <dt class="text-muted-foreground">
              Saved by compression{compressionRatio !== null ? ` (${compressionRatio}%)` : ''}
            </dt>
            <dd class="font-medium">&minus;{formatBytes(storage.compression_saved_bytes)}</dd>
          </div>
          <div class="flex items-baseline justify-between gap-4 border-t pt-3">
            <dt>On disk</dt>
            <dd class="font-medium">{formatBytes(storage.stored_bytes)}</dd>
          </div>
        </dl>
      </Card.Content>
    </Card.Root>

    <Card.Root>
      <Card.Header>
        <div class="flex items-center gap-2">
          <Card.Title>Verification</Card.Title>
          <InfoTip
            label="Why a blob can be unverified"
            text="Verification happens during compaction rather than on write, so freshly stored objects are unverified until the next pass reaches them. A failed check is the one that needs attention: it means stored bytes no longer match the hash they were written under."
          />
        </div>
        <Card.Description>Content-hash checks over the physical objects.</Card.Description>
      </Card.Header>
      <Card.Content>
        <dl class="grid gap-5 sm:grid-cols-3">
          <div>
            <dt class="text-muted-foreground text-xs">Verified</dt>
            <dd class="mt-1 text-xl">{verifiedBlobs.toLocaleString('en-US')}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs">Awaiting a check</dt>
            <dd class="mt-1 text-xl">{storage.unverified_blobs.toLocaleString('en-US')}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs">Failed</dt>
            <dd class="mt-1 text-xl">{storage.failed_integrity_blobs.toLocaleString('en-US')}</dd>
          </div>
        </dl>
        {#if storage.failed_integrity_blobs > 0}
          <p class="text-destructive mt-4 text-sm">
            Stored bytes no longer match the hash they were written under. Restore these objects
            from a backup before the next compaction pass.
          </p>
        {/if}
      </Card.Content>
    </Card.Root>
  </div>

  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>Storage by scope</Card.Title>
        <InfoTip
          label="Why these can exceed what is on disk"
          text="Each row counts what that exact scope set references, and two scopes sharing one file both count it in full. The totals are therefore logical, and their sum is larger than physical storage whenever content is shared."
        />
      </div>
      <Card.Description
        >Logical original-file references in each private or shared scope set.</Card.Description
      >
    </Card.Header>
    <Card.Content>
      {#if scopeStorage.length === 0}
        <p class="text-muted-foreground text-sm">No stored originals yet.</p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[560px] text-left text-sm">
            <thead>
              <tr class="border-b">
                <th class="pb-3 font-medium">Scopes</th>
                <th class="pb-3 font-medium">Revisions</th>
                <th class="pb-3 font-medium">Referenced</th>
                <th class="pb-3 font-medium">Share</th>
              </tr>
            </thead>
            <tbody>
              {#each scopeStorage as row (row.scopes.join(','))}
                <tr class="border-b last:border-0">
                  <td class="max-w-sm truncate py-3 pr-4 font-mono text-xs"
                    >{row.scopes.join(', ')}</td
                  >
                  <td class="py-3 pr-4">{row.artifact_revisions.toLocaleString('en-US')}</td>
                  <td class="py-3 pr-4">{formatBytes(row.logical_bytes)}</td>
                  <td class="text-muted-foreground py-3"
                    >{scopeBytes > 0 ? Math.round((row.logical_bytes / scopeBytes) * 100) : 0}%</td
                  >
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
