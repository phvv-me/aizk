<script lang="ts">
  import type { DashboardData, GraphNode, StageEstimate, UsagePoint } from './types';

  let { data, navigate }: { data: DashboardData; navigate: (view: 'sources' | 'findings' | 'subjects' | 'themes' | 'usage' | 'processing' | 'explore') => void } = $props();

  const metrics = $derived(data.overview ? [
    { label: 'Documents', value: data.overview.totals.documents, view: 'sources' as const, note: 'Kept text' },
    { label: 'Files', value: data.overview.totals.files, view: 'sources' as const, note: 'Preserved originals' },
    { label: 'Findings', value: data.overview.totals.findings, view: 'findings' as const, note: 'Grounded claims' },
    { label: 'Subjects', value: data.overview.totals.subjects, view: 'subjects' as const, note: 'Known entities' },
    { label: 'Themes', value: data.overview.totals.themes, view: 'themes' as const, note: 'Emergent groups' }
  ] : []);

  const usageDays = $derived.by(() => {
    const days = new Map<string, number>();
    for (const point of data.usage?.points ?? []) {
      const day = point.bucket.slice(0, 10);
      days.set(day, (days.get(day) ?? 0) + point.requests);
    }
    return [...days].sort(([left], [right]) => left.localeCompare(right)).slice(-14);
  });
  const usageMaximum = $derived(Math.max(1, ...usageDays.map(([, value]) => value)));

  function stageLabel(stage: StageEstimate): string {
    return stage.key === 'conversion'
      ? 'Source conversion'
      : stage.key === 'graph_projection'
        ? 'Graph enrichment'
        : stage.key.replaceAll('_', ' ');
  }

  function graphPoint(node: GraphNode, index: number, count: number): { x: number; y: number; r: number } {
    const angle = index * 2.399963 + node.degree * 0.17;
    const spread = 30 + Math.sqrt(index / Math.max(count, 1)) * 116;
    return {
      x: 180 + Math.cos(angle) * spread,
      y: 128 + Math.sin(angle) * spread * .62,
      r: Math.min(10, 3.5 + node.degree * .7)
    };
  }

  const graphPoints = $derived(
    new Map((data.graph?.nodes ?? []).map((node, index, nodes) => [node.id, graphPoint(node, index, nodes.length)]))
  );
</script>

<header class="page-heading">
  <div>
    <h1>Dashboard</h1>
    <span>Your memory at a glance with processing progress and recent activity.</span>
  </div>
</header>

{#if !data.overview || !data.processing || !data.usage}
  <div class="partial-warning">Some dashboard data is recovering. Available sections remain visible.</div>
{/if}

{#if data.overview}
  <section class="dashboard-section">
    <div class="section-label"><h2>Knowledge</h2><span>Visible across your private and shared scopes</span></div>
    <div class="metric-grid">
      {#each metrics as metric}
        <button class="metric-card" onclick={() => navigate(metric.view)}>
          <span>{metric.label}</span>
          <strong>{metric.value.toLocaleString('en-US')}</strong>
          <small>{metric.note}</small>
        </button>
      {/each}
    </div>
  </section>
{/if}

{#if data.processing}
  <section class="panel processing-panel">
    <header>
      <div><h2>Processing progress</h2><p>Live background work and recent throughput</p></div>
      <button onclick={() => navigate('processing')}>Open processing</button>
    </header>
    <div class="stage-list">
      {#each data.processing.stages.slice(0, 4) as stage}
        <div class="stage">
          <div class="stage-title"><strong>{stageLabel(stage)}</strong><span>{stage.progress_percent}%</span></div>
          <div class="progress"><i style={`width: ${stage.progress_percent}%`}></i></div>
          <div class="stage-facts"><span>{stage.queued.toLocaleString('en-US')} waiting</span><span>{stage.completed_24h.toLocaleString('en-US')} completed recently</span><span>{stage.throughput_per_hour.toFixed(1)} per hour</span></div>
        </div>
      {/each}
    </div>
  </section>
{/if}

<div class="dashboard-grid dashboard-grid-wide">
  {#if data.usage}
    <section class="panel usage-panel">
      <header>
        <div><p>Memory activity</p><h2>Successful operations</h2></div>
        <button onclick={() => navigate('usage')}>View usage</button>
      </header>
      <div class="usage-total">
        <strong>{data.usage.summary.requests.toLocaleString('en-US')}</strong>
        <span>requests in {data.usage.days} days</span>
      </div>
      {#if usageDays.length}
        <div class="usage-chart" aria-label="Successful operations by day">
          {#each usageDays as [day, value]}
            <div title={`${day} ${value} requests`}>
              <span style={`height: ${Math.max(6, (value / usageMaximum) * 100)}%`}></span>
              <small>{day.slice(5)}</small>
            </div>
          {/each}
        </div>
      {:else}
        <div class="empty-chart">Activity appears here as your agents use memory.</div>
      {/if}
      <dl class="usage-summary">
        <div><dt>Find</dt><dd>{data.usage.summary.finds.toLocaleString('en-US')}</dd></div>
        <div><dt>Keep</dt><dd>{data.usage.summary.keeps.toLocaleString('en-US')}</dd></div>
        <div><dt>Evidence items</dt><dd>{data.usage.summary.items.toLocaleString('en-US')}</dd></div>
      </dl>
    </section>
  {/if}

  <section class="panel graph-panel">
    <header>
      <div><p>Connected knowledge</p><h2>Memory graph</h2></div>
      <button onclick={() => navigate('explore')}>Explore graph</button>
    </header>
    {#if data.graph && data.graph.nodes.length}
      <svg viewBox="0 0 360 256" role="img" aria-label="A graph of connected subjects">
        {#each data.graph.edges as edge}
          {@const source = graphPoints.get(edge.source)}
          {@const target = graphPoints.get(edge.target)}
          {#if source && target}<line x1={source.x} y1={source.y} x2={target.x} y2={target.y} />{/if}
        {/each}
        {#each data.graph.nodes as node}
          {@const point = graphPoints.get(node.id)}
          {#if point}
            <circle cx={point.x} cy={point.y} r={point.r}>
              <title>{node.label}</title>
            </circle>
          {/if}
        {/each}
      </svg>
      <p class="graph-caption">{data.graph.nodes.length} subjects connected by {data.graph.edges.length} current findings</p>
    {:else}
      <div class="empty-chart">Connections appear after AIZK extracts grounded findings.</div>
    {/if}
  </section>
</div>

{#if data.overview}
  <div class="dashboard-grid">
    <section class="panel list-panel">
      <header><div><p>Recent work</p><h2>Documents</h2></div><button onclick={() => navigate('sources')}>Browse all</button></header>
      {#if data.overview.recent_documents.length}
        <ul>
          {#each data.overview.recent_documents.slice(0, 5) as document}
            <li><span class="file-mark">T</span><div><strong>{document.title}</strong><small>{document.kind} · {document.date}</small></div><span class="scope">{document.scopes[0] ?? 'private'}</span></li>
          {/each}
        </ul>
      {:else}<p class="empty-list">No authored documents are visible yet.</p>{/if}
    </section>
    <section class="panel list-panel">
      <header><div><p>Preserved sources</p><h2>Files</h2></div><button onclick={() => navigate('sources')}>Browse all</button></header>
      {#if data.overview.artifacts.length}
        <ul>
          {#each data.overview.artifacts.slice(0, 5) as file}
            <li><span class="file-mark">F</span><div><strong>{file.name}</strong><small>{file.date} · {file.detail}</small></div><span class:failed={file.status === 'failed'} class="scope">{file.status}</span></li>
          {/each}
        </ul>
      {:else}<p class="empty-list">No preserved files are visible yet.</p>{/if}
    </section>
  </div>
{/if}

<style>
  .page-heading { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 2rem; }
  .page-heading p, .panel header p { margin: .35rem 0 0; color: #7b8495; font-size: .68rem; }
  .page-heading h1 { margin: 0; color: #17223b; font-family: var(--font-brand); font-size: clamp(2rem, 4vw, 2.6rem); letter-spacing: -.05em; }
  .page-heading > div > span { display: block; margin-top: .45rem; color: #727b8e; font-size: .86rem; }
  .live-state { display: flex; align-items: center; gap: .45rem; border: 1px solid #dce5dc; border-radius: 999px; background: #f4faf4; padding: .45rem .7rem; color: #397044; font-size: .68rem; font-weight: 700; }
  .live-state i { width: .43rem; height: .43rem; border-radius: 50%; background: #42a45a; box-shadow: 0 0 0 4px #42a45a1c; }
  .partial-warning { margin-bottom: 1.5rem; border: 1px solid #f0ddb3; border-radius: .7rem; background: #fffaf0; padding: .8rem 1rem; color: #815a18; font-size: .78rem; }
  .dashboard-section { margin-bottom: 1.5rem; }
  .section-label { display: flex; align-items: baseline; gap: .7rem; margin-bottom: .65rem; }
  .section-label h2 { margin: 0; color: #626b7d; font-size: .67rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
  .section-label span { color: #9ba2b0; font-size: .65rem; }
  .metric-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: .7rem; }
  .metric-card { display: flex; min-width: 0; flex-direction: column; align-items: flex-start; border: 1px solid #dfd1b8; border-radius: .6rem; background: white; padding: 1rem; color: #17223b; text-align: left; cursor: pointer; transition: background .18s ease, color .18s ease; }
  .metric-card:hover { background: #fffdf8; color: #315dff; }
  .metric-card > span { color: #7b8495; font-size: .67rem; }
  .metric-card strong { margin: .25rem 0 .3rem; font-family: var(--font-brand); font-size: 1.75rem; letter-spacing: -.05em; }
  .metric-card small { color: #a0a6b2; font-size: .58rem; }
  .dashboard-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; margin-bottom: 1rem; }
  .dashboard-grid-wide { grid-template-columns: 1.35fr .65fr; }
  .panel { min-width: 0; border: 1px solid #dfd1b8; border-radius: .6rem; background: white; padding: 1.15rem; }
  .panel header { display: flex; align-items: flex-start; justify-content: space-between; gap: .75rem; margin-bottom: 1rem; }
  .panel h2 { margin: 0; color: #172033; font-family: var(--font-brand); font-size: 1rem; letter-spacing: -.025em; }
  .panel header button { border: 0; background: none; padding: .2rem 0; color: #315dff; font: inherit; font-size: .66rem; font-weight: 700; cursor: pointer; }
  .usage-total { display: flex; align-items: baseline; gap: .45rem; }
  .usage-total strong { color: #172033; font-size: 1.8rem; letter-spacing: -.05em; }
  .usage-total span { color: #939aa8; font-size: .65rem; }
  .usage-chart { display: flex; height: 10rem; align-items: stretch; gap: .35rem; border-bottom: 1px solid #e9ecf2; padding-top: 1rem; }
  .usage-chart > div { display: flex; min-width: 0; flex: 1; flex-direction: column; justify-content: flex-end; }
  .usage-chart span { display: block; min-height: .35rem; border-radius: .25rem .25rem 0 0; background: linear-gradient(180deg, #7d9bff, #315dff); }
  .usage-chart small { height: 1rem; overflow: hidden; padding-top: .25rem; color: #a0a6b2; font-family: var(--font-mono); font-size: .48rem; text-align: center; }
  .usage-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: .7rem; margin: 1rem 0 0; }
  .usage-summary div { border-left: 2px solid #dce4ff; padding-left: .6rem; }
  .usage-summary dt { color: #9aa1af; font-size: .57rem; }
  .usage-summary dd { margin: .15rem 0 0; color: #29334a; font-size: .8rem; font-weight: 700; }
  .empty-chart { display: grid; height: 10rem; place-items: center; border: 1px dashed #e2e6ed; border-radius: .65rem; color: #9aa1af; font-size: .72rem; text-align: center; }
  .graph-panel svg { display: block; width: 100%; height: 12.5rem; border-radius: .65rem; background: radial-gradient(circle at center, #f8faff, #f1f4fb); }
  .graph-panel line { stroke: #b9c6e6; stroke-width: .65; opacity: .8; }
  .graph-panel circle { fill: #315dff; stroke: white; stroke-width: 1.4; filter: drop-shadow(0 2px 2px #315dff33); }
  .graph-caption { margin: .55rem 0 0; color: #8d95a4; font-size: .62rem; text-align: center; }
  .processing-panel { margin-bottom: 1rem; }
  .stage-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem 2rem; }
  .stage-title { display: flex; justify-content: space-between; color: #343e52; font-size: .72rem; }
  .stage-title span { color: #315dff; font-family: var(--font-mono); font-size: .62rem; }
  .progress { height: .42rem; margin: .55rem 0; overflow: hidden; border-radius: 999px; background: #edf0f5; }
  .progress i { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #315dff, #8f76ff); }
  .stage-facts { display: flex; flex-wrap: wrap; gap: .65rem; color: #9299a7; font-size: .56rem; }
  .list-panel ul { margin: 0; padding: 0; list-style: none; }
  .list-panel li { display: flex; align-items: center; gap: .65rem; border-top: 1px solid #edf0f4; padding: .68rem 0; }
  .list-panel li:first-child { border-top: 0; padding-top: 0; }
  .file-mark { display: grid; width: 1.65rem; height: 1.65rem; flex: none; place-items: center; border-radius: .45rem; background: #edf1ff; color: #315dff; font-family: var(--font-mono); font-size: .58rem; }
  .list-panel li div { min-width: 0; flex: 1; }
  .list-panel li strong, .list-panel li small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .list-panel li strong { color: #3b4559; font-size: .7rem; }
  .list-panel li small { margin-top: .18rem; color: #9aa1af; font-size: .56rem; }
  .scope { max-width: 7rem; overflow: hidden; border-radius: 999px; background: #eef1f6; padding: .22rem .45rem; color: #687184; font-size: .53rem; text-overflow: ellipsis; white-space: nowrap; }
  .scope.failed { background: #fff0f1; color: #b3354b; }
  .empty-list { color: #9aa1af; font-size: .7rem; }
  @media (max-width: 1050px) { .metric-grid { grid-template-columns: repeat(3, 1fr); } .dashboard-grid-wide { grid-template-columns: 1fr; } }
  @media (max-width: 680px) { .page-heading { align-items: flex-start; } .live-state { display: none; } .metric-grid { grid-template-columns: repeat(2, 1fr); } .dashboard-grid, .stage-list { grid-template-columns: 1fr; } .section-label span { display: none; } }
</style>
