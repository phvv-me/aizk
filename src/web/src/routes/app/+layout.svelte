<script lang="ts">
  import type { Snippet } from 'svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import { navigation } from '$lib/nav';
  import { appRoutes } from '$lib/routes';
  import type { LayoutServerData } from './$types';

  let { data, children }: { data: LayoutServerData; children: Snippet } = $props();

  const externalLinks = $derived(
    data.links
      ? [
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
          { label: 'Traces', href: data.links.traces_url, description: 'Request paths in Tempo' }
        ]
      : undefined
  );
</script>

<div class="min-h-screen">
  <Sidebar
    me={data.me}
    accountUrl={data.accountUrl}
    sections={navigation(data.me.admin)}
    brandHref={appRoutes.dashboard}
    {externalLinks}
  />
  <main class="md:pl-64">
    <div class="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
      {#if !data.apiOnline}
        <p
          class="text-muted-foreground mb-6 rounded-md border border-dashed px-4 py-3 text-sm"
          role="status"
        >
          The AIZK API is unreachable right now, so totals and directories are hidden.
        </p>
      {/if}
      {@render children()}
    </div>
  </main>
</div>
