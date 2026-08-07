<script lang="ts">
  import type { Snippet } from 'svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import { adminNavigation } from '$lib/nav';
  import { adminRoutes } from '$lib/routes';
  import type { LayoutServerData } from './$types';

  let { data, children }: { data: LayoutServerData; children: Snippet } = $props();

  const externalLinks = $derived([
    {
      label: 'Logto',
      href: data.links.logto_url,
      description: 'Identity, users, roles and applications, on its own origin'
    },
    {
      label: 'Grafana',
      href: data.links.grafana_url,
      description: 'Dashboards, logs and metrics'
    },
    {
      label: 'Traces',
      href: data.links.traces_url,
      description: 'Request paths in Tempo'
    }
  ]);
</script>

<div class="min-h-screen">
  <Sidebar
    me={data.me}
    accountUrl={data.accountUrl}
    sections={adminNavigation()}
    brandHref={adminRoutes.overview}
    {externalLinks}
  />
  <main class="md:pl-64">
    <div class="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
      {@render children()}
    </div>
  </main>
</div>
