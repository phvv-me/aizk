<script lang="ts">
  let { title, description }: { title: string; description: string } = $props();
</script>

<svelte:head>
  <title>{title} - AIZK</title>
</svelte:head>

<header class="mb-8 space-y-1">
  <h1 class="text-2xl font-semibold tracking-tight">{title}</h1>
  <p class="text-muted-foreground">{description}</p>
</header>
