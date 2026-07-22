<script lang="ts">
  import { ArrowRight, FileArchive, FileText, Lightbulb, Network, Tags } from '@lucide/svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import MemoryGraph from '$lib/components/MemoryGraph.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import * as Card from '$lib/components/ui/card';
  import { appHref, appRoutes } from '$lib/routes';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const layers = $derived([
    {
      label: 'Sources',
      value: data.overview
        ? data.overview.totals.documents + data.overview.totals.files
        : undefined,
      description: 'The documents and preserved files that memory can quote.',
      href: appRoutes.sources,
      icon: FileText
    },
    {
      label: 'Findings',
      value: data.overview?.totals.findings,
      description: 'Current claims extracted from source text with provenance.',
      href: appRoutes.findings,
      icon: Lightbulb
    },
    {
      label: 'Subjects',
      value: data.overview?.totals.subjects,
      description: 'Named people, projects, places, and concepts mentioned by findings.',
      href: appRoutes.subjects,
      icon: Tags
    },
    {
      label: 'Themes',
      value: data.overview?.totals.themes,
      description: 'Broader groups discovered from related subjects and findings.',
      href: appRoutes.themes,
      icon: Network
    }
  ]);
</script>

<PageHeader
  title="Memory map"
  description="See how stored material becomes searchable knowledge and inspect each layer."
/>

<Card.Root class="mb-6">
  <Card.Header>
    <div class="flex items-center gap-2">
      <Card.Title>How AIZK organizes memory</Card.Title>
      <InfoTip
        label="How the memory layers work"
        text="AIZK keeps original sources, extracts current findings, links their named subjects, and groups related knowledge into themes. Every layer remains read only here."
      />
    </div>
    <Card.Description>
      Follow the flow from original material to higher level structure. Select any layer to inspect
      it.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    <ol class="grid gap-3 lg:grid-cols-4">
      {#each layers as layer, index (layer.label)}
        {@const Icon = layer.icon}
        <li class="relative">
          <a
            href={layer.href}
            class="hover:bg-muted/40 focus-visible:ring-ring block h-full rounded-lg border p-4 transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            <div class="mb-3 flex items-center justify-between">
              <Icon class="text-primary size-5" aria-hidden="true" />
              <span class="text-muted-foreground text-xs">Step {index + 1}</span>
            </div>
            <p class="font-medium">{layer.label}</p>
            {#if layer.value !== undefined}
              <p class="mt-1 text-2xl tabular-nums">{layer.value.toLocaleString('en-US')}</p>
            {/if}
            <p class="text-muted-foreground mt-2 text-sm leading-relaxed">{layer.description}</p>
          </a>
          {#if index < layers.length - 1}
            <ArrowRight
              class="text-muted-foreground bg-background absolute top-1/2 -right-3 z-10 hidden size-5 -translate-y-1/2 rounded-full p-0.5 lg:block"
              aria-hidden="true"
            />
          {/if}
        </li>
      {/each}
    </ol>
  </Card.Content>
</Card.Root>

{#if data.overview}
  <section aria-labelledby="source-kinds-heading" class="mb-6">
    <div class="mb-3 flex items-center gap-2">
      <h2 id="source-kinds-heading" class="font-medium">Two kinds of source</h2>
      <InfoTip
        label="Documents and files"
        text="Documents are text remembered directly. Files are preserved originals such as PDFs, images, and fetched web pages. AIZK converts files into text without losing the original."
      />
    </div>
    <div class="grid gap-4 sm:grid-cols-2">
      <a href={appHref(appRoutes.sources, { origin: 'document' })}>
        <Card.Root class="hover:bg-muted/30 h-full transition-colors">
          <Card.Header>
            <FileText class="text-primary size-5" aria-hidden="true" />
            <Card.Title
              >{data.overview.totals.documents.toLocaleString('en-US')} documents</Card.Title
            >
            <Card.Description>Text remembered directly through AIZK clients.</Card.Description>
          </Card.Header>
        </Card.Root>
      </a>
      <a href={appHref(appRoutes.sources, { origin: 'file' })}>
        <Card.Root class="hover:bg-muted/30 h-full transition-colors">
          <Card.Header>
            <FileArchive class="text-primary size-5" aria-hidden="true" />
            <Card.Title>{data.overview.totals.files.toLocaleString('en-US')} files</Card.Title>
            <Card.Description>Uploaded or fetched originals preserved by AIZK.</Card.Description>
          </Card.Header>
        </Card.Root>
      </a>
    </div>
  </section>
{/if}

{#if data.graph}
  <Card.Root>
    <Card.Content class="pt-6">
      <MemoryGraph graph={data.graph} />
    </Card.Content>
  </Card.Root>
{:else}
  <Card.Root>
    <Card.Header>
      <Card.Title>Relationship graph unavailable</Card.Title>
      <Card.Description
        >The catalogs above remain available while graph data recovers.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{/if}
