<script lang="ts">
  import InfoTip from './InfoTip.svelte';

  let {
    title,
    description,
    items
  }: {
    title: string;
    description: string;
    items: Array<{ label: string; value: number }>;
  } = $props();

  const maximum = $derived(Math.max(1, ...items.map((item) => item.value)));
</script>

<figure class="space-y-4">
  <figcaption>
    <div class="flex items-center gap-2 font-medium">
      {title}
      <InfoTip label={`How to read ${title}`} text={description} />
    </div>
    <p class="text-muted-foreground mt-1 text-sm">{description}</p>
  </figcaption>
  {#if items.length === 0}
    <p class="text-muted-foreground text-sm">No values are available for this view.</p>
  {:else}
    <div class="space-y-3" aria-label={title}>
      {#each items as item (item.label)}
        <div class="grid grid-cols-[minmax(7rem,12rem)_1fr_auto] items-center gap-3">
          <span class="truncate text-sm" title={item.label}>{item.label}</span>
          <div class="bg-muted h-3 rounded-full">
            <div
              class="bg-primary h-3 rounded-full"
              style={`width: ${Math.max(2, (item.value / maximum) * 100)}%`}
              title={`${item.label} ${item.value.toLocaleString('en-US')}`}
            ></div>
          </div>
          <span class="text-muted-foreground w-16 text-right text-sm tabular-nums">
            {item.value.toLocaleString('en-US')}
          </span>
        </div>
      {/each}
    </div>
    <details class="text-sm">
      <summary class="text-muted-foreground hover:text-foreground cursor-pointer"
        >Show data table</summary
      >
      <table class="mt-3 w-full text-left">
        <caption class="sr-only">{description}</caption>
        <thead>
          <tr class="border-b">
            <th class="py-2 font-medium">Name</th>
            <th class="py-2 text-right font-medium">Value</th>
          </tr>
        </thead>
        <tbody>
          {#each items as item (item.label)}
            <tr class="border-b last:border-0">
              <td class="py-2">{item.label}</td>
              <td class="py-2 text-right tabular-nums">{item.value.toLocaleString('en-US')}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </details>
  {/if}
</figure>
