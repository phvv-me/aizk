<script lang="ts">
  import type { AppView, JsonValue } from './types';

  let { view, data }: { view: AppView; data: JsonValue } = $props();

  type Row = Record<string, JsonValue>;

  function record(value: JsonValue): Row | null {
    return value !== null && !Array.isArray(value) && typeof value === 'object' ? value : null;
  }

  function rows(value: JsonValue): JsonValue[] {
    if (Array.isArray(value)) return value;
    const root = record(value);
    if (!root) return [];
    for (const key of ['rows', 'organizations', 'stages', 'points', 'edges', 'recent']) {
      const found = root[key];
      if (Array.isArray(found)) return found;
    }
    return [];
  }

  function text(value: JsonValue | undefined): string {
    if (value === null || value === undefined) return '';
    if (Array.isArray(value)) return value.map((item) => text(item)).filter(Boolean).join(', ');
    if (typeof value === 'object') return '';
    return String(value);
  }

  function title(item: Row, index: number): string {
    for (const key of ['title', 'name', 'label', 'statement', 'key', 'operation']) {
      const found = text(item[key]);
      if (found) return found;
    }
    return `${view} ${index + 1}`;
  }

  function details(item: Row): Array<[string, string]> {
    return Object.entries(item)
      .filter(([key, value]) => !['id', 'title', 'name', 'label', 'statement'].includes(key) && text(value))
      .slice(0, 6)
      .map(([key, value]) => [key.replaceAll('_', ' '), text(value)]);
  }

  const root = $derived(record(data));
  const visibleRows = $derived(rows(data));
  const markdown = $derived(text(root?.markdown));
</script>

<section class="resource-view">
  <header>
    <div>
      <strong>{visibleRows.length ? `${visibleRows.length} visible items` : 'Current result'}</strong>
      <span>Authenticated and limited to your private and shared scopes</span>
    </div>
  </header>

  {#if markdown}
    <article class="answer">{markdown}</article>
  {:else if visibleRows.length}
    <div class="row-grid">
      {#each visibleRows as value, index}
        {@const item = record(value)}
        {#if item}
          <article class="row-card">
            <h2>{title(item, index)}</h2>
            {#if text(item.summary)}<p>{text(item.summary)}</p>{/if}
            <dl>
              {#each details(item) as [key, value]}
                <div><dt>{key}</dt><dd>{value}</dd></div>
              {/each}
            </dl>
          </article>
        {:else}
          <article class="row-card"><p>{text(value)}</p></article>
        {/if}
      {/each}
    </div>
  {:else if root}
    <dl class="summary-grid">
      {#each Object.entries(root) as [key, value]}
        {#if text(value)}<div><dt>{key.replaceAll('_', ' ')}</dt><dd>{text(value)}</dd></div>{/if}
      {/each}
    </dl>
  {:else}
    <p class="empty">Nothing is visible in this section yet.</p>
  {/if}
</section>

<style>
  .resource-view { margin-top: 1rem; overflow: hidden; border: 1px solid #dfd1b8; border-radius: .6rem; background: white; }
  .resource-view > header { border-bottom: 1px solid #eee4d3; padding: 1rem 1.2rem; }
  .resource-view > header div { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
  .resource-view > header strong { color: #17223b; font-size: .78rem; }
  .resource-view > header span { color: #7c8494; font-size: .65rem; }
  .answer { padding: 1.3rem; color: #29334a; font-family: var(--font-mono); font-size: .76rem; line-height: 1.75; white-space: pre-wrap; }
  .row-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; background: #eee4d3; }
  .row-card { min-width: 0; background: white; padding: 1.2rem; }
  .row-card h2 { margin: 0; color: #17223b; font-size: .92rem; line-height: 1.45; }
  .row-card p { margin: .65rem 0 0; color: #626b7d; font-size: .75rem; line-height: 1.6; }
  dl { margin: .85rem 0 0; }
  dl div { display: grid; grid-template-columns: minmax(5rem, .35fr) 1fr; gap: .7rem; border-top: 1px solid #f0e8da; padding: .48rem 0; }
  dt { color: #8e7f6a; font-size: .58rem; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
  dd { min-width: 0; margin: 0; overflow: hidden; color: #4e586d; font-size: .65rem; text-overflow: ellipsis; }
  .summary-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0 1.5rem; margin: 0; padding: 1.2rem; }
  .empty { margin: 0; padding: 2rem; color: #7c8494; font-size: .78rem; text-align: center; }
  @media (max-width: 760px) { .row-grid, .summary-grid { grid-template-columns: 1fr; } .resource-view > header div { align-items: flex-start; flex-direction: column; } }
</style>
