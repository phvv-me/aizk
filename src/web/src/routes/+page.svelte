<script lang="ts">
  import { Sparkles } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
</script>

<svelte:head>
  <title>AIZK</title>
  <meta name="description" content="AIZK is the shared memory engine for you and your agents." />
</svelte:head>

<main class="flex min-h-screen flex-col items-center justify-center gap-10 px-6 text-center">
  <div class="max-w-xl space-y-5">
    <p
      class="text-muted-foreground inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm"
    >
      <Sparkles class="text-primary size-4" aria-hidden="true" />
      AI Zettelkasten
    </p>
    <h1 class="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
      One memory for you, your team and your agents
    </h1>
    <p class="text-muted-foreground text-lg text-pretty">
      AIZK remembers what you and your agents learn, recalls it on demand, ingests your documents
      and shares knowledge across your organizations.
    </p>
  </div>
  <div class="flex flex-wrap items-center justify-center gap-3">
    <Button href="/auth/sign-in" size="lg">Sign in</Button>
    <Button href="/auth/sign-up" size="lg" variant="outline">Create account</Button>
  </div>
</main>
