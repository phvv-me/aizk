<script lang="ts">
  import * as Card from '$lib/components/ui/card';

  let { title, unreachable }: { title: string; unreachable: boolean } = $props();
</script>

<Card.Root>
  <Card.Header>
    <Card.Title>{title} is unavailable</Card.Title>
    <Card.Description>
      {#if unreachable}
        This will return once the AIZK API answers again.
      {:else}
        Nothing has been measured yet. A worker takes this reading on a schedule, so it appears
        after its first pass.
      {/if}
    </Card.Description>
  </Card.Header>
</Card.Root>
