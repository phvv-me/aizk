<script lang="ts">
  import { deserialize, enhance } from '$app/forms';
  import { invalidateAll } from '$app/navigation';
  import { Link, RefreshCw, UploadCloud } from '@lucide/svelte';
  import { toast } from 'svelte-sonner';
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

  let fileInput: HTMLInputElement;
  let uploading = $state(false);
  let intaking = $state(false);
  let dragging = $state(false);
  let refreshing = $state(false);

  const statusVariants: Record<ArtifactView['status'], BadgeVariant> = {
    queued: 'outline',
    processing: 'secondary',
    ready: 'default',
    failed: 'destructive'
  };

  /** Ask the server action for a capability grant, then PUT the bytes same-origin. */
  async function upload(file: File) {
    if (file.size === 0) {
      toast.error('Choose a non-empty file to upload.');
      return;
    }
    uploading = true;
    try {
      const path = await grantPath(file);
      if (!path) return;
      const put = await fetch(path, {
        method: 'PUT',
        headers: { 'content-type': file.type || 'application/octet-stream' },
        body: file
      });
      if (!put.ok) {
        toast.error(await putDetail(put));
        return;
      }
      toast.success('File accepted for processing.');
      await invalidateAll();
    } catch {
      toast.error('The AIZK API is unreachable right now. Please try again.');
    } finally {
      uploading = false;
      fileInput.value = '';
    }
  }

  /** Declare the file to the `grant` action and read back the same-origin PUT path. */
  async function grantPath(file: File): Promise<string | null> {
    const declaration = new FormData();
    declaration.set('filename', file.name);
    declaration.set('media_type', file.type || 'application/octet-stream');
    declaration.set('size', String(file.size));
    const response = await fetch('?/grant', {
      method: 'POST',
      headers: { 'x-sveltekit-action': 'true' },
      body: declaration
    });
    const result = deserialize<{ path: string }, { message: string }>(await response.text());
    if (result.type === 'success' && result.data?.path) return result.data.path;
    const message = result.type === 'failure' ? result.data?.message : undefined;
    toast.error(message ?? 'The upload could not be authorized.');
    return null;
  }

  async function putDetail(response: Response): Promise<string> {
    try {
      const body = (await response.json()) as { detail?: string };
      return body.detail ?? `The upload failed with status ${response.status}.`;
    } catch {
      return `The upload failed with status ${response.status}.`;
    }
  }

  function drop(event: DragEvent) {
    event.preventDefault();
    dragging = false;
    const file = event.dataTransfer?.files?.[0];
    if (file) void upload(file);
  }

  async function refresh() {
    refreshing = true;
    await invalidateAll();
    refreshing = false;
  }
</script>

<PageHeader
  title="Sources"
  description="Upload documents or intake https links and follow their processing."
/>

<div class="mb-8 grid gap-4 lg:grid-cols-2">
  <div>
    <input
      type="file"
      name="file"
      id="file"
      class="sr-only"
      bind:this={fileInput}
      onchange={() => {
        const file = fileInput.files?.[0];
        if (file) void upload(file);
      }}
    />
    <label
      for="file"
      class={cn(
        'border-border hover:border-ring hover:bg-accent/40 flex h-full min-h-40 cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-8 text-center transition-colors',
        dragging && 'border-ring bg-accent/40',
        uploading && 'pointer-events-none opacity-60'
      )}
      aria-label="Upload a document"
      ondragover={(event) => {
        event.preventDefault();
        dragging = true;
      }}
      ondragleave={() => (dragging = false)}
      ondrop={drop}
    >
      <UploadCloud class="text-muted-foreground size-8" aria-hidden="true" />
      <span class="text-sm font-medium">
        {uploading ? 'Uploading' : 'Drop a document here or click to browse'}
      </span>
      <span class="text-muted-foreground text-xs">
        Files are scanned and converted before they join your memory.
      </span>
    </label>
  </div>

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
          No files or links yet. Upload a document above to get started.
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
