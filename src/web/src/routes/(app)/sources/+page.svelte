<script lang="ts">
  import { enhance } from '$app/forms';
  import { invalidateAll } from '$app/navigation';
  import { Link, RefreshCw } from '@lucide/svelte';
  import type { ArtifactView } from '$lib/api';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import { Badge, type BadgeVariant } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Input } from '$lib/components/ui/input';
  import { Label } from '$lib/components/ui/label';
  import { feedback } from '$lib/forms';
  import { cn, webHref } from '$lib/utils';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  let intaking = $state(false);
  let refreshing = $state(false);

  const statusVariants: Record<ArtifactView['status'], BadgeVariant> = {
    queued: 'outline',
    processing: 'secondary',
    ready: 'default',
    failed: 'destructive'
  };

  async function refresh() {
    refreshing = true;
    await invalidateAll();
    refreshing = false;
  }
</script>

<PageHeader title="Sources" description="Intake https links and follow their processing." />

<div class="mb-8">
  <Card.Root>
    <Card.Header>
      <Card.Title>Intake a link</Card.Title>
      <Card.Description>Remember a page or document reachable over https.</Card.Description>
    </Card.Header>
    <Card.Content>
      <form
        method="POST"
        action="?/intake"
        class="space-y-3"
        use:enhance={feedback('Link accepted for processing.', {
          pending: (active) => (intaking = active)
        })}
      >
        <Label for="source_uri">Https link</Label>
        <Input
          id="source_uri"
          name="source_uri"
          type="url"
          required
          placeholder="https://example.com/paper.pdf"
        />
        <div class="flex justify-end">
          <Button type="submit" variant="secondary" disabled={intaking}>
            <Link aria-hidden="true" />
            {intaking ? 'Intaking' : 'Intake'}
          </Button>
        </div>
      </form>
    </Card.Content>
  </Card.Root>
</div>

<section aria-label="Processing states">
  <Card.Root>
    <Card.Header>
      <Card.Title>Processing</Card.Title>
      <Card.Description>Your latest originals and where they stand.</Card.Description>
      <Card.Action>
        <Button variant="ghost" size="sm" onclick={refresh} disabled={refreshing}>
          <RefreshCw class={cn(refreshing && 'animate-spin')} aria-hidden="true" />
          Refresh
        </Button>
      </Card.Action>
    </Card.Header>
    <Card.Content>
      {#if !data.overview}
        <p class="text-muted-foreground text-sm">
          Processing states will appear once the AIZK API answers again.
        </p>
      {:else if data.overview.artifacts.length === 0}
        <p class="text-muted-foreground text-sm">
          No links yet. Intake a link above to get started.
        </p>
      {:else}
        <ul class="divide-border divide-y">
          {#each data.overview.artifacts as artifact, index (index)}
            {@const href = webHref(artifact.source_uri)}
            <li class="flex flex-wrap items-center gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0">
              <div class="min-w-0 flex-1">
                {#if href}
                  <a
                    {href}
                    target="_blank"
                    rel="noreferrer"
                    class="hover:text-primary block truncate text-sm font-medium underline-offset-4 hover:underline"
                  >
                    {artifact.name}
                  </a>
                {:else}
                  <p class="truncate text-sm font-medium">{artifact.name}</p>
                {/if}
                <p class="text-muted-foreground text-xs">{artifact.detail} · {artifact.date}</p>
              </div>
              <ScopeBadges scopes={artifact.scopes} />
              <Badge variant={statusVariants[artifact.status]}>{artifact.status}</Badge>
            </li>
          {/each}
        </ul>
      {/if}
    </Card.Content>
  </Card.Root>
</section>
