<script lang="ts">
  import { browser } from '$app/environment';
  import DOMPurify from 'dompurify';
  import { marked } from 'marked';

  let { source }: { source: string } = $props();

  const html = $derived(browser ? DOMPurify.sanitize(marked.parse(source, { async: false })) : '');
</script>

{#if browser}
  <div class="prose prose-sm dark:prose-invert max-w-none">
    <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized above -->
    {@html html}
  </div>
{:else}
  <pre class="text-sm whitespace-pre-wrap">{source}</pre>
{/if}
