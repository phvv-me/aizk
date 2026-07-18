<script lang="ts">
  import '../app.css';
  import { Toaster } from 'svelte-sonner';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
</script>

<Toaster richColors position="top-right" />
{@render children()}
