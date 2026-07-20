<script lang="ts">
  import HorizontalBars from '$lib/components/HorizontalBars.svelte';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ScopeBadges from '$lib/components/ScopeBadges.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import { formatDateTime } from '$lib/format';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();

  const sizes = $derived(
    (data.themes?.rows ?? []).map((theme) => ({ label: theme.label, value: theme.member_count }))
  );
</script>

<PageHeader
  title="Themes"
  description="Explore graph communities that summarize related subjects and findings."
/>

{#if !data.themes}
  <Card.Root>
    <Card.Header>
      <Card.Title>Themes unavailable</Card.Title>
      <Card.Description
        >The theme catalog will return once the AIZK API answers again.</Card.Description
      >
    </Card.Header>
  </Card.Root>
{:else if data.themes.rows.length === 0}
  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <Card.Title>No themes yet</Card.Title>
        <InfoTip
          label="When themes appear"
          text="Themes are rebuilt after enough new findings accumulate. Sources can be recallable before the next theme pass finishes."
        />
      </div>
      <Card.Description>
        Themes will appear after the graph has enough related subjects to form useful communities.
      </Card.Description>
    </Card.Header>
  </Card.Root>
{:else}
  <Card.Root class="mb-6">
    <Card.Content class="pt-6">
      <HorizontalBars
        title="Theme sizes"
        description="Each bar counts the subjects assigned to a visible theme. A larger theme covers more subjects, not necessarily a more important idea."
        items={sizes}
      />
    </Card.Content>
  </Card.Root>

  <div class="grid gap-5 lg:grid-cols-2">
    {#each data.themes.rows as theme (theme.id)}
      <Card.Root>
        <Card.Header>
          <div class="flex items-center gap-2">
            <Card.Title>{theme.label}</Card.Title>
            <InfoTip
              label={`How to read ${theme.label}`}
              text="The summary is generated from the visible graph community. Member names are a bounded preview, and the member count covers the full theme."
            />
          </div>
          <Card.Description>
            {theme.member_count.toLocaleString('en-US')} subjects · updated {formatDateTime(
              theme.updated_at
            )}
          </Card.Description>
          <Card.Action><ScopeBadges scopes={theme.scopes} /></Card.Action>
        </Card.Header>
        <Card.Content class="space-y-4">
          <p class="text-sm leading-relaxed">{theme.summary}</p>
          {#if theme.members.length > 0}
            <div>
              <p class="text-muted-foreground mb-2 text-xs font-medium uppercase">Member preview</p>
              <div class="flex flex-wrap gap-2">
                {#each theme.members as member (member)}
                  <a href={`/subjects?search=${encodeURIComponent(member)}`}>
                    <Badge variant="secondary" class="hover:bg-accent">{member}</Badge>
                  </a>
                {/each}
              </div>
            </div>
          {/if}
        </Card.Content>
      </Card.Root>
    {/each}
  </div>
{/if}
