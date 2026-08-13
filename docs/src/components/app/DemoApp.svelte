<script lang="ts">
  import LogtoClient from '@logto/browser';
  import { onMount } from 'svelte';

  type AppConfig = {
    logtoEndpoint: string;
    appId: string;
    resource: string;
    callbackPath: string;
  };

  type View = 'overview' | 'recall' | 'sources' | 'findings' | 'subjects' | 'themes' | 'graph' | 'processing' | 'usage' | 'organizations';

  let config: AppConfig | undefined;
  let client: LogtoClient | undefined;
  let authenticated = false;
  let loading = true;
  let error = '';
  let result = '';
  let query = '';
  let view: View = 'overview';

  const views: Array<{ id: View; label: string }> = [
    { id: 'overview', label: 'Overview' },
    { id: 'recall', label: 'Recall' },
    { id: 'sources', label: 'Sources' },
    { id: 'findings', label: 'Findings' },
    { id: 'subjects', label: 'Subjects' },
    { id: 'themes', label: 'Themes' },
    { id: 'graph', label: 'Graph' },
    { id: 'processing', label: 'Processing' },
    { id: 'usage', label: 'Usage' },
    { id: 'organizations', label: 'Organizations' }
  ];

  onMount(async () => {
    try {
      config = (await fetch('/app-config.json').then((response) => response.json())) as AppConfig;
      client = new LogtoClient({
        endpoint: config.logtoEndpoint,
        appId: config.appId,
        scopes: ['openid', 'profile', 'email', 'offline_access', 'control'],
        resources: [config.resource]
      });
      if (await client.isSignInRedirected(window.location.href)) {
        await client.handleSignInCallback(window.location.href);
        window.history.replaceState({}, '', '/app/dashboard');
      }
      authenticated = await client.isAuthenticated();
      if (authenticated) await load('overview');
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'The application could not start.';
    } finally {
      loading = false;
    }
  });

  async function token(): Promise<string> {
    if (!client || !config) throw new Error('Authentication is not ready.');
    return client.getAccessToken(config.resource);
  }

  async function request(path: string, init: RequestInit = {}): Promise<unknown> {
    const response = await fetch(path, {
      ...init,
      headers: {
        authorization: `Bearer ${await token()}`,
        'content-type': 'application/json',
        ...init.headers
      }
    });
    const body = (await response.json()) as { detail?: string };
    if (!response.ok) throw new Error(body.detail ?? `AIZK returned ${response.status}.`);
    return body;
  }

  async function load(next: View): Promise<void> {
    view = next;
    error = '';
    result = '';
    try {
      if (next === 'recall') return;
      if (next === 'overview') {
        const [me, overview] = await Promise.all([request('/api/me'), request('/api/overview')]);
        result = JSON.stringify({ me, overview }, null, 2);
        return;
      }
      result = JSON.stringify(await request(`/api/${next}`), null, 2);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'The request failed.';
    }
  }

  async function recall(): Promise<void> {
    error = '';
    result = '';
    try {
      result = JSON.stringify(
        await request('/api/recall', { method: 'POST', body: JSON.stringify({ query }) }),
        null,
        2
      );
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Recall failed.';
    }
  }

  async function signIn(): Promise<void> {
    if (!client || !config) return;
    await client.signIn({
      redirectUri: `${window.location.origin}${config.callbackPath}`,
      postRedirectUri: `${window.location.origin}/app/dashboard`
    });
  }

  async function signOut(): Promise<void> {
    await client?.signOut(`${window.location.origin}/`);
  }
</script>

<main class="min-h-screen bg-slate-950 text-slate-100">
  <header class="border-b border-slate-800 bg-slate-950/90">
    <div class="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
      <a class="text-xl font-bold tracking-tight" href="/">aizk</a>
      <div class="flex items-center gap-3">
        <a class="text-sm text-slate-300 hover:text-white" href="/docs/">Docs</a>
        {#if authenticated}
          <button class="rounded-lg border border-slate-700 px-3 py-2 text-sm" onclick={signOut}>Sign out</button>
        {/if}
      </div>
    </div>
  </header>

  {#if loading}
    <p class="mx-auto max-w-7xl px-5 py-16 text-slate-400">Opening crAIZK…</p>
  {:else if !authenticated}
    <section class="mx-auto max-w-xl px-5 py-24 text-center">
      <p class="mb-3 text-sm font-semibold uppercase tracking-widest text-indigo-400">CockroachDB demo</p>
      <h1 class="text-4xl font-bold">Shared memory for your team and agents</h1>
      <p class="mt-5 text-slate-300">Create an account or sign in to start using crAIZK.</p>
      <button class="mt-8 rounded-xl bg-indigo-500 px-5 py-3 font-semibold hover:bg-indigo-400" onclick={signIn}>Create account or sign in</button>
      {#if error}<p class="mt-5 text-sm text-rose-300">{error}</p>{/if}
    </section>
  {:else}
    <div class="mx-auto grid max-w-7xl gap-6 px-5 py-6 lg:grid-cols-[220px_1fr]">
      <nav class="flex gap-2 overflow-x-auto lg:flex-col" aria-label="Application">
        {#each views as item}
          <button
            class="rounded-lg px-3 py-2 text-left text-sm whitespace-nowrap {view === item.id ? 'bg-indigo-500 font-semibold' : 'text-slate-300 hover:bg-slate-800'}"
            onclick={() => load(item.id)}
          >{item.label}</button>
        {/each}
      </nav>
      <section class="min-w-0 rounded-2xl border border-slate-800 bg-slate-900 p-6">
        <h1 class="text-2xl font-bold capitalize">{view}</h1>
        {#if view === 'recall'}
          <form class="mt-5 flex gap-3" onsubmit={(event) => { event.preventDefault(); recall(); }}>
            <input class="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-4 py-3" bind:value={query} required placeholder="What does the team know about…" />
            <button class="rounded-lg bg-indigo-500 px-5 py-3 font-semibold" type="submit">Ask</button>
          </form>
        {/if}
        {#if error}<p class="mt-5 rounded-lg bg-rose-950 p-4 text-rose-200">{error}</p>{/if}
        {#if result}<pre class="mt-5 max-h-[65vh] overflow-auto rounded-xl bg-slate-950 p-5 text-sm leading-6 text-slate-200">{result}</pre>{/if}
      </section>
    </div>
  {/if}
</main>
