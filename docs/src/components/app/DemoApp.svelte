<script lang="ts">
  import LogtoClient from '@logto/browser';
  import { onMount } from 'svelte';
  import AppSidebar from './AppSidebar.svelte';
  import DashboardView from './DashboardView.svelte';
  import ResourceView from './ResourceView.svelte';
  import type {
    AppView,
    DashboardData,
    GraphSlice,
    JsonValue,
    Me,
    Overview,
    ProcessingReport,
    UsageReport
  } from './types';

  type AppConfig = {
    logtoEndpoint: string;
    appId: string;
    resource: string;
    callbackPath: string;
  };

  const resources: Record<Exclude<AppView, 'overview' | 'recall'>, string> = {
    explore: '/api/graph?limit=40',
    sources: '/api/sources',
    findings: '/api/findings',
    subjects: '/api/subjects',
    themes: '/api/themes',
    usage: '/api/usage?days=30',
    processing: '/api/processing',
    organizations: '/api/organizations'
  };
  const titles: Record<AppView, string> = {
    overview: 'Dashboard',
    recall: 'Recall',
    explore: 'Explore',
    sources: 'Sources',
    findings: 'Findings',
    subjects: 'Subjects',
    themes: 'Themes',
    usage: 'Usage',
    processing: 'Processing',
    organizations: 'Organizations'
  };

  let config: AppConfig | undefined;
  let client: LogtoClient | undefined;
  let authenticated = false;
  let loading = true;
  let contentLoading = false;
  let error = '';
  let result: JsonValue | null = null;
  let query = '';
  let view: AppView = 'overview';
  let me: Me | null = null;
  let dashboard: DashboardData | null = null;

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

  async function request<T extends JsonValue>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, {
      ...init,
      headers: {
        authorization: `Bearer ${await token()}`,
        'content-type': 'application/json',
        ...init.headers
      }
    });
    const body = (await response.json()) as T & { detail?: string };
    if (!response.ok) throw new Error(body.detail ?? `AIZK returned ${response.status}.`);
    return body;
  }

  async function load(next: AppView): Promise<void> {
    view = next;
    error = '';
    result = null;
    contentLoading = true;
    try {
      if (next === 'recall') return;
      if (next === 'overview') {
        const responses = await Promise.allSettled([
          request<Me>('/api/me'),
          request<Overview>('/api/overview'),
          request<ProcessingReport>('/api/processing'),
          request<UsageReport>('/api/usage?days=30'),
          request<GraphSlice>('/api/graph?limit=40')
        ]);
        const identity = responses[0];
        if (identity.status === 'rejected') throw identity.reason;
        me = identity.value;
        dashboard = {
          me,
          overview: responses[1].status === 'fulfilled' ? responses[1].value : null,
          processing: responses[2].status === 'fulfilled' ? responses[2].value : null,
          usage: responses[3].status === 'fulfilled' ? responses[3].value : null,
          graph: responses[4].status === 'fulfilled' ? responses[4].value : null
        };
        return;
      }
      result = await request<JsonValue>(resources[next]);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'The request failed.';
    } finally {
      contentLoading = false;
    }
  }

  async function recall(): Promise<void> {
    error = '';
    result = null;
    contentLoading = true;
    try {
      result = await request<JsonValue>('/api/recall', {
        method: 'POST',
        body: JSON.stringify({ query })
      });
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Recall failed.';
    } finally {
      contentLoading = false;
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

  function itemCount(value: JsonValue): number | null {
    if (Array.isArray(value)) return value.length;
    if (value && typeof value === 'object') {
      const list = Object.values(value).find(Array.isArray);
      return list?.length ?? null;
    }
    return null;
  }
</script>

<svelte:head><title>{authenticated ? `${titles[view]} · aizk` : 'Sign in · aizk'}</title></svelte:head>

{#if loading}
  <main class="startup-screen">
    <img src="/brain-box-icon.webp" alt="" />
    <span>Opening your AI Zettelkasten</span>
  </main>
{:else if !authenticated}
  <main class="sign-in-screen">
    <a href="/" class="signin-brand"><img src="/brain-box-icon.webp" alt="" /><span><strong>aizk</strong><small>AI Zettelkasten</small></span></a>
    <section>
      <div class="signin-cube"><img src="/brain-box.webp" alt="A memory block formed from interlocking brain folds" /></div>
      <div class="signin-copy">
        <p>Your knowledge workspace</p>
        <h1>Memory that stays connected</h1>
        <span>Sign in to recall what your team knows, inspect sources and follow every connection back to evidence.</span>
        <button onclick={signIn}>Create account or sign in</button>
        <a href="/docs/user/quickstart/">Read the quickstart</a>
        {#if error}<div class="auth-error">{error}</div>{/if}
      </div>
    </section>
  </main>
{:else if me}
  <div class="app-shell">
    <AppSidebar {view} {me} navigate={load} {signOut} />
    <main class="app-main">
      {#if view === 'overview' && dashboard}
        <DashboardView data={dashboard} navigate={load} />
      {:else}
        <header class="resource-heading">
          <div><p>AI Zettelkasten</p><h1>{titles[view]}</h1></div>
          {#if result !== null && itemCount(result) !== null}<span>{itemCount(result)} visible items</span>{/if}
        </header>

        {#if view === 'recall'}
          <section class="recall-card">
            <p>Ask across every source you can access. AIZK keeps evidence and scope attached.</p>
            <form onsubmit={(event) => { event.preventDefault(); recall(); }}>
              <input bind:value={query} required placeholder="What does the team know about…" />
              <button type="submit" disabled={contentLoading}>{contentLoading ? 'Searching' : 'Ask memory'}</button>
            </form>
          </section>
        {/if}

        {#if contentLoading}
          <div class="loading-panel"><i></i><span>Loading {titles[view].toLowerCase()}</span></div>
        {/if}
        {#if error}<div class="request-error">{error}</div>{/if}
        {#if result !== null}<ResourceView {view} data={result} />{/if}
      {/if}
    </main>
  </div>
{:else}
  <main class="startup-screen"><span>{error || 'Opening your dashboard'}</span></main>
{/if}

<style>
  :global(body) { margin: 0; background: #fffaf1; font-family: var(--font-sans); }
  .startup-screen { display: grid; min-height: 100vh; place-content: center; gap: 1rem; color: #697287; text-align: center; }
  .startup-screen img { width: 4rem; margin: auto; border-radius: 1.2rem; animation: breathe 1.6s ease-in-out infinite alternate; }
  @keyframes breathe { to { transform: translateY(-5px); box-shadow: 0 16px 40px #315dff38; } }
  .sign-in-screen { min-height: 100vh; background: #f7f8fb; padding: 1.25rem; color: #172033; }
  .signin-brand { display: inline-flex; align-items: center; gap: .7rem; color: inherit; text-decoration: none; }
  .signin-brand img { width: 2.25rem; border-radius: .65rem; }
  .signin-brand span { display: flex; flex-direction: column; }
  .signin-brand strong { letter-spacing: -.03em; }
  .signin-brand small { color: #8991a1; font-size: .6rem; }
  .sign-in-screen > section { display: grid; width: min(60rem, 100%); min-height: 34rem; margin: 5vh auto 0; grid-template-columns: 1fr 1fr; overflow: hidden; border: 1px solid #e0e4eb; border-radius: 1.4rem; background: white; box-shadow: 0 30px 80px #25304a14; }
  .signin-cube { min-height: 28rem; overflow: hidden; background: radial-gradient(circle, #dae4ff, #f2f4f9); }
  .signin-cube img { width: 100%; height: 100%; object-fit: cover; }
  .signin-copy { display: flex; flex-direction: column; justify-content: center; padding: clamp(2rem, 6vw, 4rem); }
  .signin-copy > p, .resource-heading p { margin: 0 0 .5rem; color: #315dff; font-size: .65rem; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
  .signin-copy h1 { margin: 0; font-family: var(--font-display); font-size: clamp(2.7rem, 5vw, 4.3rem); font-weight: 400; line-height: .94; letter-spacing: -.04em; }
  .signin-copy > span { margin: 1.4rem 0; color: #6f788a; font-size: .85rem; line-height: 1.65; }
  .signin-copy button { border: 0; border-radius: .7rem; background: #315dff; padding: .8rem 1rem; color: white; font: inherit; font-size: .78rem; font-weight: 800; cursor: pointer; }
  .signin-copy > a { margin-top: .9rem; color: #6c7587; font-size: .7rem; text-align: center; text-underline-offset: 3px; }
  .auth-error, .request-error { margin-top: 1rem; border: 1px solid #f1cbd1; border-radius: .6rem; background: #fff4f5; padding: .7rem .8rem; color: #a43248; font-size: .7rem; }
  .app-shell { min-height: 100vh; color: #172033; }
  .app-main { box-sizing: border-box; width: min(calc(100% - 16rem), 96rem); min-height: 100vh; margin-left: 16rem; padding: clamp(1.25rem, 4vw, 3rem); }
  .resource-heading { display: flex; align-items: end; justify-content: space-between; margin-bottom: 1.5rem; }
  .resource-heading h1 { margin: 0; font-family: var(--font-brand); font-size: 2.5rem; letter-spacing: -.05em; }
  .resource-heading > span { border: 1px solid #dfe3eb; border-radius: 999px; background: white; padding: .4rem .7rem; color: #727b8d; font-size: .65rem; }
  .recall-card, .loading-panel { border: 1px solid #dfd1b8; border-radius: .6rem; background: white; padding: 1.2rem; }
  .recall-card p { margin: 0 0 1rem; color: #778093; font-size: .75rem; }
  .recall-card form { display: flex; gap: .7rem; }
  .recall-card input { min-width: 0; flex: 1; border: 1px solid #dce1e9; border-radius: .65rem; background: #f8f9fb; padding: .8rem 1rem; color: #172033; font: inherit; }
  .recall-card button { border: 0; border-radius: .65rem; background: #315dff; padding: .75rem 1.1rem; color: white; font: inherit; font-size: .75rem; font-weight: 700; cursor: pointer; }
  .loading-panel { display: flex; align-items: center; gap: .7rem; color: #7c8494; font-size: .75rem; }
  .loading-panel i { width: .8rem; height: .8rem; border: 2px solid #d6def8; border-top-color: #315dff; border-radius: 50%; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 767px) { .app-main { width: 100%; margin-left: 0; padding: 1.25rem 1rem 2rem; } .sign-in-screen > section { margin-top: 2rem; grid-template-columns: 1fr; } .signin-cube { min-height: 17rem; max-height: 17rem; } .signin-copy { padding: 2rem; } .recall-card form { flex-direction: column; } }
</style>
