<script lang="ts">
  import type { GraphSlice } from '$lib/api';
  import InfoTip from './InfoTip.svelte';

  let { graph }: { graph: GraphSlice } = $props();

  const width = 800;
  const height = 480;
  const visibleNodes = $derived(graph.nodes.slice(0, 24));
  const positions = $derived.by(() => {
    const result = new Map<string, { x: number; y: number }>();
    const radius = Math.min(width, height) * 0.38;
    visibleNodes.forEach((node, index) => {
      const angle = (index / Math.max(1, visibleNodes.length)) * Math.PI * 2 - Math.PI / 2;
      result.set(node.id, {
        x: width / 2 + Math.cos(angle) * radius,
        y: height / 2 + Math.sin(angle) * radius
      });
    });
    return result;
  });
  const visibleEdges = $derived(
    graph.edges.filter((edge) => positions.has(edge.source) && positions.has(edge.target))
  );
  const labels = $derived(new Map(graph.nodes.map((node) => [node.id, node.label])));
</script>

<figure class="space-y-4">
  <figcaption>
    <div class="flex items-center gap-2 font-medium">
      Relationship graph
      <InfoTip
        label="How to read the relationship graph"
        text="Each circle is a subject. A line is a current finding that connects two subjects. Larger circles participate in more visible findings. The view is intentionally bounded to stay readable. Use the relationship list for every exact statement."
      />
    </div>
    <p class="text-muted-foreground mt-1 text-sm">
      A bounded view of recent subject relationships. Select a node to filter the Subjects page.
    </p>
  </figcaption>
  {#if visibleNodes.length === 0}
    <p class="text-muted-foreground text-sm">No binary relationships are visible yet.</p>
  {:else}
    <div class="bg-muted/20 overflow-hidden rounded-lg border">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        class="h-auto w-full"
        role="img"
        aria-labelledby="graph-title"
      >
        <title id="graph-title"
          >Recent memory relationships with {visibleNodes.length} subjects and {visibleEdges.length} findings</title
        >
        {#each visibleEdges as edge, index (`${edge.source}-${edge.target}-${index}`)}
          {@const source = positions.get(edge.source)}
          {@const target = positions.get(edge.target)}
          {#if source && target}
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              class="stroke-border"
              stroke-width="1.5"
            >
              <title>{labels.get(edge.source)} {edge.predicate} {labels.get(edge.target)}</title>
            </line>
          {/if}
        {/each}
        {#each visibleNodes as node, index (node.id)}
          {@const point = positions.get(node.id)}
          {#if point}
            <a
              href={`/subjects?search=${encodeURIComponent(node.label)}`}
              aria-label={`${node.label} with ${node.degree} relationships`}
            >
              <circle
                cx={point.x}
                cy={point.y}
                r={Math.min(18, 7 + node.degree)}
                fill="var(--series-1)"
                class="stroke-card focus-visible:stroke-ring cursor-pointer"
                stroke-width="3"
              >
                <title>{node.label} with {node.degree} relationships</title>
              </circle>
            </a>
            {#if index < 12}
              <text
                x={point.x}
                y={point.y + 28}
                text-anchor="middle"
                class="fill-foreground text-[11px]">{node.label.slice(0, 24)}</text
              >
            {/if}
          {/if}
        {/each}
      </svg>
    </div>
    {#if graph.truncated || graph.nodes.length > visibleNodes.length}
      <p class="text-muted-foreground text-xs">
        This graph is bounded for readability. The relationship list below contains the exact
        visible edges in this slice.
      </p>
    {/if}
    <details class="text-sm" open>
      <summary class="text-muted-foreground hover:text-foreground cursor-pointer"
        >Relationship list</summary
      >
      <ul class="mt-3 divide-y">
        {#each visibleEdges as edge, index (`list-${edge.source}-${edge.target}-${index}`)}
          <li class="py-2">
            <span class="font-medium">{labels.get(edge.source)}</span>
            <span class="text-muted-foreground"> {edge.predicate} </span>
            <span class="font-medium">{labels.get(edge.target)}</span>
            <p class="text-muted-foreground mt-1 text-xs">{edge.statement}</p>
          </li>
        {/each}
      </ul>
    </details>
  {/if}
</figure>
