<script lang="ts">
  import type { Snippet } from 'svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import type { LayoutServerData } from './$types';

  let { data, children }: { data: LayoutServerData; children: Snippet } = $props();
</script>

<div class="min-h-screen">
  <Sidebar me={data.me} accountUrl={data.accountUrl} />
  <main class="md:pl-64">
    <div class="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
      {#if !data.apiOnline}
        <p
          class="text-muted-foreground mb-6 rounded-md border border-dashed px-4 py-3 text-sm"
          role="status"
        >
          The AIZK API is unreachable right now, so totals and directories are hidden.
        </p>
      {/if}
      {@render children()}
    </div>
  </main>
</div>
