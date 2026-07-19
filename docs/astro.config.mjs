import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import mermaid from 'astro-mermaid';

// https://astro.build/config
export default defineConfig({
  site: 'https://phvv.me',
  base: '/aizk',
  integrations: [
    // astro-mermaid must be registered before Starlight so its markdown
    // transform runs first. autoTheme swaps the diagram theme with the page.
    mermaid({
      theme: 'neutral',
      autoTheme: true,
    }),
    starlight({
      title: 'aizk',
      description: 'Self-hosted shared memory for people, teams, and MCP agents.',
      logo: {
        src: './src/assets/icon.svg',
        alt: 'aizk',
      },
      favicon: '/favicon.svg',
      customCss: ['./src/styles/brand.css'],
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/phvv-me/aizk' },
      ],
      sidebar: [
        {
          label: 'Getting Started',
          items: [
            { label: 'Home', link: '/' },
            { label: 'Quickstart', slug: 'quickstart' },
            { label: 'Concepts', slug: 'concepts' },
            { label: 'Onboarding', slug: 'onboarding' },
          ],
        },
        {
          label: 'Engine',
          items: [
            { label: 'The engine', slug: 'engine' },
            { label: 'The store', slug: 'engine/store' },
            { label: 'The write path', slug: 'engine/write-path' },
            { label: 'The read path', slug: 'engine/read-path' },
            { label: 'The scope-set lattice', slug: 'engine/lattice' },
            { label: 'Identity and sharing', slug: 'engine/identity' },
            { label: 'Autonomy', slug: 'engine/autonomy' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Architecture rules', slug: 'architecture' },
            { label: 'API', slug: 'api' },
            { label: 'Comparison', slug: 'comparison' },
            { label: 'Benchmarks', slug: 'benchmarks' },
            { label: 'References', slug: 'references' },
          ],
        },
        {
          label: 'Operations',
          items: [
            { label: 'Operations', slug: 'operations' },
            { label: 'Security', slug: 'security' },
            { label: 'MCP clients', slug: 'mcp-clients' },
            { label: 'Release', slug: 'release' },
          ],
        },
      ],
    }),
  ],
});
