<script lang="ts">
  import { SvelteFlow, Background, Controls } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import { flows } from './flows';

  // Svelte Flow measures nodes with the browser layout engine, so this component must be mounted
  // `client:only="svelte"` and its container must have a real height or the canvas collapses.
  let { flow = 'system', height = '30rem' }: { flow?: string; height?: string } = $props();

  const source = flows[flow];
  let nodes = $state.raw(source.nodes);
  let edges = $state.raw(source.edges);
  let selected = $state(source.initial);
  const current = $derived(source.detail[selected]);
</script>

<div class="not-content grid gap-4 lg:grid-cols-[1fr_20rem]">
  <!-- overflow-hidden matters. Nodes are absolutely positioned, so on a narrow screen one sitting
       to the right of the fitted viewport would otherwise widen the whole page. -->
  <div style:height class="overflow-hidden border border-current/15">
    <SvelteFlow
      bind:nodes
      bind:edges
      fitView
      colorMode="system"
      nodesDraggable={false}
      onnodeclick={({ node }) => (selected = node.id)}
    >
      <Background patternColor="currentColor" gap={18} />
      <Controls showLock={false} />
    </SvelteFlow>
  </div>

  <aside class="flex flex-col gap-3 border border-current/15 p-5">
    <p class="font-mono text-xs opacity-50">Click any box</p>
    <h3 class="text-lg font-semibold">{current.title}</h3>
    <p class="text-sm opacity-80">{current.body}</p>
    <a href={current.href} class="mt-auto text-sm font-medium underline underline-offset-4">
      Read more
    </a>
  </aside>
</div>

<style>
  /* Svelte Flow ships its own node chrome. These rules square it off and drop the default blue
     so it matches the monochrome brand. */
  :global(.aizk-node) {
    border-radius: 0;
    border-color: currentColor;
    font-size: 0.8rem;
  }
  :global(.svelte-flow__node.selected .aizk-node),
  :global(.svelte-flow__node:focus-visible .aizk-node) {
    outline: 2px solid currentColor;
  }
</style>
