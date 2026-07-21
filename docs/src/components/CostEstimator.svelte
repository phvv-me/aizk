<script lang="ts">
  // A sizing aid, not a benchmark. Every constant below is either read straight out of
  // `src/deploy/docker-compose.yml` and `src/aizk/config/settings.py`, or taken from a dated
  // measurement cell that the throughput note names. Anything the code does not pin down is
  // presented as a range and labeled as such.

  type Model = { label: string; vram: number; note: string };

  const extractors: Model[] = [
    { label: 'Gemma 4 12B QAT w4a16', vram: 10.3, note: 'current production extractor' },
    { label: 'Gemma 4 31B w4a16', vram: 22.8, note: 'previous baseline, best measured grounding' },
    { label: 'Gemma 4 E2B w4a16', vram: 9.5, note: 'lower resource, 68.6% judged faithfulness' },
    { label: 'GLiNER2 large only', vram: 2.4, note: 'no LLM lane, cheap but weaker edges' },
  ];

  const embedders: Model[] = [
    { label: 'Qwen3-VL-Embedding-2B', vram: 4.7, note: 'pooling runner, 0.30 of a 24 GB card' },
  ];

  const rerankers: Model[] = [
    { label: 'Qwen3-Reranker-4B fp8', vram: 6.0, note: 'pooling runner, 0.25 of a 24 GB card' },
    { label: 'none', vram: 0, note: 'recall falls back to statement order past the fusion stage' },
  ];

  const gates: Model[] = [
    { label: 'GLiNER2 large', vram: 2.4, note: 'shared relevance gate and mention seeder' },
    { label: 'GLiNER2 base', vram: 1.5, note: 'cheaper, fewer relations recovered' },
  ];

  let extractor = $state(extractors[0]);
  let embedder = $state(embedders[0]);
  let reranker = $state(rerankers[0]);
  let gate = $state(gates[0]);
  let cardGb = $state(24);
  let cards = $state(2);
  let docsPerMonth = $state(500);
  let pagesPerDoc = $state(6);

  // Chunking is 2048 tokens at 4.0 characters per token, and a dense page runs about 3,000
  // characters, so a page is a bit over a third of a chunk.
  const CHARS_PER_CHUNK = 2048 * 4.0;
  const CHARS_PER_PAGE = 3000;
  const EMBEDDING_BYTES = 1024 * 2; // halfvec(1024)

  const chunks = $derived(Math.ceil((docsPerMonth * pagesPerDoc * CHARS_PER_PAGE) / CHARS_PER_CHUNK));

  // The shipped Compose file splits by size rather than by role. The three small lanes share one
  // card and the extractor takes the other at 0.97 utilization. With a single card everything has
  // to pack together instead, which is the case worth warning about.
  const smallLanes = $derived(embedder.vram + reranker.vram + gate.vram);
  const dedicated = $derived(cards >= 2);
  const llmCard = $derived(dedicated ? extractor.vram : 0);
  const packedCard = $derived(dedicated ? smallLanes : smallLanes + extractor.vram);
  const cardsNeeded = $derived(smallLanes + extractor.vram <= cardGb * 0.95 ? 1 : 2);
  const fits = $derived(
    dedicated
      ? smallLanes <= cardGb * 0.95 && extractor.vram <= cardGb * 0.97
      : packedCard <= cardGb * 0.95,
  );

  // Two dated cells bracket extraction. A warm vLLM lane with continuous batching amortized to
  // about 667 ms per chunk, while single-chunk smoke tests with no batching ran 20 to 75 seconds.
  // Real steady-state work sits near the batched end, so that is the low bound here.
  const secondsPerChunk = $derived(extractor.label.startsWith('GLiNER') ? [0.3, 2.9] : [0.7, 4.5]);
  const hoursLow = $derived((chunks * secondsPerChunk[0]) / 3600);
  const hoursHigh = $derived((chunks * secondsPerChunk[1]) / 3600);

  // Text, its embedding, and the derived rows it produces. Entities and facts are embedded too,
  // and the observed ratio is roughly one entity and one fact per chunk after consolidation.
  const storageGb = $derived(
    (chunks * (CHARS_PER_CHUNK + EMBEDDING_BYTES * 3) * 12) / 1024 ** 3,
  );

  const number = (value: number, digits = 1) =>
    value.toLocaleString('en-US', { maximumFractionDigits: digits });
</script>

<div class="not-content flex flex-col gap-6 border border-current/15 p-5">
  <div class="grid gap-4 sm:grid-cols-2">
    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Extractor</span>
      <select bind:value={extractor} class="border border-current/20 bg-transparent p-2 text-sm">
        {#each extractors as option (option.label)}
          <option value={option}>{option.label}</option>
        {/each}
      </select>
      <span class="text-xs opacity-60">{extractor.note}</span>
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Reranker</span>
      <select bind:value={reranker} class="border border-current/20 bg-transparent p-2 text-sm">
        {#each rerankers as option (option.label)}
          <option value={option}>{option.label}</option>
        {/each}
      </select>
      <span class="text-xs opacity-60">{reranker.note}</span>
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Gate</span>
      <select bind:value={gate} class="border border-current/20 bg-transparent p-2 text-sm">
        {#each gates as option (option.label)}
          <option value={option}>{option.label}</option>
        {/each}
      </select>
      <span class="text-xs opacity-60">{gate.note}</span>
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Embedder</span>
      <select bind:value={embedder} class="border border-current/20 bg-transparent p-2 text-sm">
        {#each embedders as option (option.label)}
          <option value={option}>{option.label}</option>
        {/each}
      </select>
      <span class="text-xs opacity-60">{embedder.note}</span>
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">VRAM per card, GB <span class="opacity-60">{cardGb}</span></span>
      <input type="range" min="12" max="96" step="4" bind:value={cardGb} />
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Cards <span class="opacity-60">{cards}</span></span>
      <input type="range" min="1" max="4" step="1" bind:value={cards} />
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Documents per month <span class="opacity-60">{docsPerMonth}</span></span>
      <input type="range" min="50" max="5000" step="50" bind:value={docsPerMonth} />
    </label>

    <label class="flex flex-col gap-1 text-sm">
      <span class="font-medium">Pages per document <span class="opacity-60">{pagesPerDoc}</span></span>
      <input type="range" min="1" max="60" step="1" bind:value={pagesPerDoc} />
    </label>
  </div>

  <table class="w-full text-sm">
    <tbody>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Model card</th>
        <td class="py-2 text-right font-mono">
          {dedicated ? `${number(llmCard)} GB extractor alone` : 'shared with the small lanes'}
        </td>
      </tr>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Shared card</th>
        <td class="py-2 text-right font-mono">{number(packedCard)} GB</td>
      </tr>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Cards needed</th>
        <td class="py-2 text-right font-mono">{cardsNeeded}</td>
      </tr>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Chunks per month</th>
        <td class="py-2 text-right font-mono">{number(chunks, 0)}</td>
      </tr>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Extraction wall clock</th>
        <td class="py-2 text-right font-mono">{number(hoursLow)} to {number(hoursHigh)} hours</td>
      </tr>
      <tr class="border-t border-current/10">
        <th class="py-2 text-left font-normal opacity-60">Database growth</th>
        <td class="py-2 text-right font-mono">{number(storageGb, 2)} GB per year</td>
      </tr>
    </tbody>
  </table>

  <p class="border-l-2 border-current/30 pl-3 text-sm">
    {#if fits && dedicated}
      This fits. The extractor gets a card to itself and the three small lanes share the other,
      which is the layout the shipped Compose file assumes.
    {:else if fits}
      This fits on one card of {cardGb} GB, with every lane packed together.
    {:else if !dedicated}
      This does not fit on one card. All four lanes need {number(packedCard)} GB together, so
      either add a second card, raise the card size, or drop to a smaller extractor.
    {:else}
      This does not fit. The extractor needs {number(llmCard)} GB and the small lanes need
      {number(packedCard)} GB, and one of the two is over a {cardGb} GB card.
    {/if}
  </p>

  <p class="text-xs opacity-60">
    Weights only. vLLM also reserves a KV cache out of the same card, which the shipped Compose
    file controls through per-service utilization fractions rather than absolute sizes, so treat
    these as floors. The throughput range comes from two measurements under different serving
    configurations, an amortized 667 ms per chunk on a warm batched lane and 20 to 75 seconds per
    chunk on unbatched single-chunk smoke tests, so it is a bracket rather than a prediction.
  </p>
</div>
