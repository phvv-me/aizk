<script lang="ts">
  import { enhance } from '$app/forms';
  import { Sparkles } from '@lucide/svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import Markdown from '$lib/components/Markdown.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Label } from '$lib/components/ui/label';
  import { Textarea } from '$lib/components/ui/textarea';
  import { feedback } from '$lib/forms';
  import type { ActionData } from './$types';

  let { form }: { form: ActionData } = $props();
  let asking = $state(false);
</script>

<PageHeader title="Find" description="Ask your memory a question and read the grounded evidence." />

<form
  method="POST"
  class="mb-8 space-y-3"
  use:enhance={feedback('Found.', { reset: false, pending: (active) => (asking = active) })}
>
  <div class="flex items-center gap-2">
    <Label for="query">Question</Label>
    <InfoTip
      label="How Find works"
      text="Find searches source excerpts, current findings, themes, and recent session memory that your scopes allow. Evidence is ordered by merit and includes provenance labels."
    />
  </div>
  <Textarea
    id="query"
    name="query"
    required
    rows={3}
    placeholder="What do we know about ..."
    value={form?.query ?? ''}
  />
  <div class="flex items-center justify-end gap-3">
    {#if form?.message}
      <p role="alert" class="text-destructive flex-1 text-sm">{form.message}</p>
    {/if}
    <Button type="submit" disabled={asking}>
      <Sparkles aria-hidden="true" />
      {asking ? 'Finding' : 'Find'}
    </Button>
  </div>
</form>

{#if asking}
  <Card.Root aria-busy="true">
    <Card.Content>
      <p class="text-muted-foreground animate-pulse text-sm" role="status">
        Searching your memory.
      </p>
    </Card.Content>
  </Card.Root>
{:else if form?.markdown !== undefined}
  <Card.Root aria-live="polite">
    <Card.Content>
      {#if form.markdown}
        <Markdown source={form.markdown} />
      {:else}
        <p class="text-muted-foreground text-sm" role="status">
          Nothing found for this question yet. Keep sources through a connected AIZK client and try
          again.
        </p>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}
