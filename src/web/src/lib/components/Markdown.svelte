<script lang="ts">
  import { browser } from '$app/environment';
  import DOMPurify from 'dompurify';
  import { marked } from 'marked';
  import { webHref } from '$lib/utils';

  let { source }: { source: string } = $props();

  const html = $derived.by(() => {
    if (!browser) return '';
    const sanitized = DOMPurify.sanitize(marked.parse(source, { async: false }));
    const document = new DOMParser().parseFromString(sanitized, 'text/html');
    document.querySelectorAll('a').forEach((anchor) => {
      const href = anchor.getAttribute('href');
      if (href?.startsWith('#')) return;
      const safe = webHref(href ?? '');
      if (safe) {
        anchor.href = safe;
        anchor.target = '_blank';
        anchor.rel = 'noreferrer';
        return;
      }
      anchor.removeAttribute('href');
      anchor.title = 'The original source did not provide an absolute link';
    });
    document.querySelectorAll('img').forEach((image) => {
      const safe = webHref(image.getAttribute('src') ?? '');
      if (safe) {
        image.src = safe;
        return;
      }
      image.removeAttribute('src');
      image.title = 'The original source did not provide an absolute image link';
    });
    return document.body.innerHTML;
  });
</script>

{#if browser}
  <div class="prose prose-sm dark:prose-invert max-w-none">
    <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized above -->
    {@html html}
  </div>
{:else}
  <pre class="text-sm whitespace-pre-wrap">{source}</pre>
{/if}
