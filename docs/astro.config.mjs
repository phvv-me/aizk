import svelte from '@astrojs/svelte';
import starlight from '@astrojs/starlight';
import tailwind from '@tailwindcss/vite';
import { defineConfig } from 'astro/config';
import d2 from 'astro-d2';

const site = process.env.AIZK_DOCS_SITE_URL ?? 'https://aizk.example.com';

// The whole product lives on one origin. A plain Astro page owns `/`, and Astro gives static
// routes priority over Starlight's dynamic `[...slug]`, so the marketing page wins without a
// redirect. Starlight has no base option of its own, so every docs page is nested one level
// deeper under `src/content/docs/docs/` to come out at `/docs/...`.
export default defineConfig({
  site,
  vite: { plugins: [tailwind()] },
  integrations: [
    // D2 draws what mermaid draws badly, the table shapes and the container nesting. useD2js
    // renders through WebAssembly so no D2 binary has to exist in the build container.
    d2({
      experimental: { useD2js: true },
      layout: 'elk',
      pad: 20,
      // Diagrams stay neutral so readers never confuse brand decoration with data encoding.
      // Theme 200 is the closest dark counterpart to neutral theme 1.
      theme: { default: '1', dark: '200' },
    }),
    svelte(),
    starlight({
      title: 'aizk',
      description: 'Shared memory for people, teams, and AI agents.',
      logo: { src: './src/assets/icon.svg', alt: 'aizk' },
      favicon: '/favicon.svg',
      head: [
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
        {
          tag: 'link',
          attrs: {
            rel: 'preconnect',
            href: 'https://fonts.gstatic.com',
            crossorigin: true,
          },
        },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Manrope:wght@400;500;600;700&display=swap',
          },
        },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' } },
        { tag: 'meta', attrs: { name: 'theme-color', content: '#315dff' } },
        {
          tag: 'meta',
          attrs: { property: 'og:image', content: `${site}/social-card.png` },
        },
        {
          tag: 'meta',
          attrs: {
            property: 'og:image:alt',
            content: 'aizk · shared memory for people, teams, and AI agents',
          },
        },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
        {
          tag: 'meta',
          attrs: { name: 'twitter:image', content: `${site}/social-card.png` },
        },
      ],
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
            { label: 'How settled a claim is', slug: 'docs/user/concepts/settledness' },
            { label: 'Who maintains memory', slug: 'docs/user/concepts/lifecycle' },
          ],
        },
        {
          label: 'Using aizk',
          items: [
            { label: 'Writing memory well', slug: 'docs/user/using/remember' },
            { label: 'Asking memory well', slug: 'docs/user/using/recall' },
            { label: 'Finding on the web', slug: 'docs/user/using/web' },
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
            { label: 'Web boilerplate', slug: 'docs/dev/write/boilerplate' },
            { label: 'OCR and languages', slug: 'docs/dev/write/ocr' },
            { label: 'Chunking and embedding', slug: 'docs/dev/write/chunking' },
            { label: 'Extraction and the gate', slug: 'docs/dev/write/extraction' },
            { label: 'Grounding and consolidation', slug: 'docs/dev/write/consolidation' },
            { label: 'Settledness and contradiction', slug: 'docs/dev/write/certainty' },
          ],
        },
        {
          label: 'Autonomous passes',
          badge: { text: 'dev', variant: 'note' },
          items: [
            { label: 'The job system', slug: 'docs/dev/passes/jobs' },
            { label: 'Communities', slug: 'docs/dev/passes/communities' },
            { label: 'RAPTOR', slug: 'docs/dev/passes/raptor' },
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
            { label: 'Database profiles', slug: 'docs/dev/run/databases' },
            { label: 'AWS and CockroachDB Cloud', slug: 'docs/dev/run/aws' },
            { label: 'Self-hosted topology', slug: 'docs/dev/run/topology' },
            { label: 'Hardware and cost', slug: 'docs/dev/run/hardware' },
            { label: 'PostgreSQL first start', slug: 'docs/dev/run/first-start' },
            { label: 'PostgreSQL profile', slug: 'docs/dev/run/postgres' },
            { label: 'Object storage and compression', slug: 'docs/dev/run/object-store' },
            { label: 'PostgreSQL backups', slug: 'docs/dev/run/backups' },
            { label: 'The operator console', slug: 'docs/dev/run/console' },
            { label: 'Observability', slug: 'docs/dev/run/observability' },
            { label: 'Telemetry', slug: 'docs/dev/run/telemetry' },
            { label: 'Upgrades', slug: 'docs/dev/run/upgrades' },
            { label: 'Self-hosted security', slug: 'docs/dev/run/security' },
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
            { label: 'Brand and visual identity', slug: 'docs/dev/contributing/brand' },
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
