import svelte from '@astrojs/svelte';
import starlight from '@astrojs/starlight';
import tailwind from '@tailwindcss/vite';
import { defineConfig } from 'astro/config';
import d2 from 'astro-d2';
import mermaid from 'astro-mermaid';

// The whole product lives on one origin. A plain Astro page owns `/`, and Astro gives static
// routes priority over Starlight's dynamic `[...slug]`, so the marketing page wins without a
// redirect. Starlight has no base option of its own, so every docs page is nested one level
// deeper under `src/content/docs/docs/` to come out at `/docs/...`.
export default defineConfig({
  site: 'https://aizk.phvv.me',
  vite: { plugins: [tailwind()] },
  integrations: [
    // astro-mermaid must be registered before Starlight so its markdown transform runs first.
    // autoTheme swaps the diagram theme with the page.
    mermaid({ theme: 'neutral', autoTheme: true }),
    // D2 draws what mermaid draws badly, the table shapes and the container nesting. useD2js
    // renders through WebAssembly so no D2 binary has to exist in the build container.
    d2({
      experimental: { useD2js: true },
      layout: 'elk',
      pad: 20,
      // Theme 1 is D2's neutral grey. The brand carries no accent hue, so a diagram must not
      // introduce one, and 200 is the closest dark counterpart.
      theme: { default: '1', dark: '200' },
    }),
    svelte(),
    starlight({
      title: 'aizk',
      description: 'Self-hosted shared memory for people, teams, and MCP agents.',
      logo: { src: './src/assets/icon.svg', alt: 'aizk' },
      favicon: '/favicon.svg',
      customCss: ['./src/styles/docs.css'],
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/phvv-me/aizk' }],
      sidebar: [
        { label: 'Documentation home', slug: 'docs' },
        {
          label: 'Start here',
          items: [
            { label: 'What aizk is', slug: 'docs/user/what-is-aizk' },
            { label: 'Quickstart', slug: 'docs/user/quickstart' },
            { label: 'Your first hour', slug: 'docs/user/first-hour' },
          ],
        },
        {
          label: 'Concepts',
          items: [
            { label: 'Sources and derived knowledge', slug: 'docs/user/concepts/sources' },
            { label: 'Scopes', slug: 'docs/user/concepts/scopes' },
            { label: 'Time and history', slug: 'docs/user/concepts/time' },
            { label: 'Entities, facts, ontology', slug: 'docs/user/concepts/graph' },
            { label: 'Evidence and provenance', slug: 'docs/user/concepts/evidence' },
            { label: 'Who maintains memory', slug: 'docs/user/concepts/lifecycle' },
          ],
        },
        {
          label: 'Using aizk',
          items: [
            { label: 'Writing memory well', slug: 'docs/user/using/remember' },
            { label: 'Asking memory well', slug: 'docs/user/using/recall' },
            { label: 'Files, PDFs and web sources', slug: 'docs/user/using/files' },
            { label: 'Sharing and organizations', slug: 'docs/user/using/sharing' },
            { label: 'The web app', slug: 'docs/user/using/web-app' },
            { label: 'Notes that stay useful', slug: 'docs/user/using/habits' },
          ],
        },
        {
          label: 'Connect a client',
          items: [
            { label: 'Claude Code', slug: 'docs/user/clients/claude-code' },
            { label: 'Codex', slug: 'docs/user/clients/codex' },
            { label: 'OpenCode', slug: 'docs/user/clients/opencode' },
            { label: 'Sign-in troubleshooting', slug: 'docs/user/clients/troubleshooting' },
          ],
        },
        {
          label: 'User reference',
          items: [
            { label: 'MCP tools', slug: 'docs/user/reference/tools' },
            { label: 'Glossary', slug: 'docs/user/reference/glossary' },
            { label: 'Questions and answers', slug: 'docs/user/reference/faq' },
          ],
        },
        {
          label: 'Architecture',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'System map', slug: 'docs/dev/architecture/system-map' },
            { label: 'Layers and import contracts', slug: 'docs/dev/architecture/layers' },
            { label: 'Repository tour', slug: 'docs/dev/architecture/repository' },
            { label: 'Design principles', slug: 'docs/dev/architecture/principles' },
          ],
        },
        {
          label: 'The store',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'The data model', slug: 'docs/dev/store/data-model' },
            { label: 'Content and artifact tables', slug: 'docs/dev/store/content-tables' },
            { label: 'Graph tables', slug: 'docs/dev/store/graph-tables' },
            { label: 'The bi-temporal model', slug: 'docs/dev/store/bitemporal' },
            { label: 'Row level security', slug: 'docs/dev/store/rls' },
            { label: 'Migrations and DDL', slug: 'docs/dev/store/migrations' },
          ],
        },
        {
          label: 'The write path',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'Intake', slug: 'docs/dev/write/intake' },
            { label: 'Artifacts', slug: 'docs/dev/write/artifacts' },
            { label: 'Chunking and embedding', slug: 'docs/dev/write/chunking' },
            { label: 'Extraction and the gate', slug: 'docs/dev/write/extraction' },
            { label: 'Grounding and consolidation', slug: 'docs/dev/write/consolidation' },
          ],
        },
        {
          label: 'Autonomous passes',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'The job system', slug: 'docs/dev/passes/jobs' },
            { label: 'Communities and RAPTOR', slug: 'docs/dev/passes/communities-raptor' },
            { label: 'Profiles, insights, decay', slug: 'docs/dev/passes/profiles-insights' },
            { label: 'Promotion and sharing', slug: 'docs/dev/passes/promotion' },
          ],
        },
        {
          label: 'The read path',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'How recall runs', slug: 'docs/dev/read/overview' },
            { label: 'The lanes', slug: 'docs/dev/read/lanes' },
            { label: 'Fusion and reranking', slug: 'docs/dev/read/ranking' },
            { label: 'Budget packing', slug: 'docs/dev/read/packing' },
            { label: 'Retrieval tuning', slug: 'docs/dev/read/tuning' },
          ],
        },
        {
          label: 'Identity',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'The Logto boundary', slug: 'docs/dev/identity/logto' },
            { label: 'Scope sets in depth', slug: 'docs/dev/identity/scope-sets' },
            { label: 'Background work', slug: 'docs/dev/identity/background' },
          ],
        },
        {
          label: 'Interfaces',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'The MCP server', slug: 'docs/dev/interfaces/mcp' },
            { label: 'The HTTP API', slug: 'docs/dev/interfaces/http-api' },
            { label: 'The CLI', slug: 'docs/dev/interfaces/cli' },
            { label: 'The web app', slug: 'docs/dev/interfaces/web' },
          ],
        },
        {
          label: 'Running aizk',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'Deployment topology', slug: 'docs/dev/run/topology' },
            { label: 'Hardware and cost', slug: 'docs/dev/run/hardware' },
            { label: 'First start', slug: 'docs/dev/run/first-start' },
            { label: 'PostgreSQL and storage', slug: 'docs/dev/run/postgres' },
            { label: 'Backups and recovery', slug: 'docs/dev/run/backups' },
            { label: 'Observability', slug: 'docs/dev/run/observability' },
            { label: 'Upgrades', slug: 'docs/dev/run/upgrades' },
            { label: 'The security model', slug: 'docs/dev/run/security' },
            { label: 'The release gate', slug: 'docs/dev/run/release-gate' },
          ],
        },
        {
          label: 'Evaluation',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'How we evaluate', slug: 'docs/dev/eval/approach' },
            { label: 'The eval CLI', slug: 'docs/dev/eval/cli' },
            { label: 'Retrieval results', slug: 'docs/dev/eval/retrieval' },
            { label: 'Extraction and models', slug: 'docs/dev/eval/extraction' },
            { label: 'External benchmarks', slug: 'docs/dev/eval/external' },
          ],
        },
        {
          label: 'Prior art',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'Comparison', slug: 'docs/dev/prior-art/comparison' },
            { label: 'References and lineage', slug: 'docs/dev/prior-art/references' },
            { label: 'Rejected and deferred', slug: 'docs/dev/prior-art/rejected' },
          ],
        },
        {
          label: 'Contributing',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'Development setup', slug: 'docs/dev/contributing/setup' },
            { label: 'Testing', slug: 'docs/dev/contributing/testing' },
            { label: 'Style and typing', slug: 'docs/dev/contributing/style' },
            { label: 'Writing these docs', slug: 'docs/dev/contributing/docs-style' },
            { label: 'Releasing', slug: 'docs/dev/contributing/release' },
          ],
        },
      ],
    }),
  ],
});
