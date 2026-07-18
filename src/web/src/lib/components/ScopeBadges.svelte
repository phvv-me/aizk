<script lang="ts">
  import { Badge } from '$lib/components/ui/badge';

  let { scopes }: { scopes: string[] } = $props();
</script>

<span class="flex flex-wrap gap-1">
  {#each scopes as scope (scope)}
    <Badge variant={scope === 'Private' ? 'outline' : 'secondary'}>{scope}</Badge>
  {/each}
</span>
