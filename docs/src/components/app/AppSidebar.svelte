<script lang="ts">
  import type { AppView, Me } from './types';

  let {
    view,
    me,
    navigate,
    signOut
  }: {
    view: AppView;
    me: Me;
    navigate: (view: AppView) => void;
    signOut: () => Promise<void>;
  } = $props();

  const sections: Array<{
    label: string;
    links: Array<{ id: AppView; label: string; glyph: string }>;
  }> = [
    {
      label: 'Knowledge',
      links: [
        { id: 'overview', label: 'Dashboard', glyph: 'D' },
        { id: 'find', label: 'Find', glyph: 'F' }
      ]
    },
    {
      label: 'Memory',
      links: [
        { id: 'explore', label: 'Memory map', glyph: 'M' },
        { id: 'sources', label: 'Sources', glyph: 'S' },
        { id: 'findings', label: 'Findings', glyph: 'F' },
        { id: 'subjects', label: 'Subjects', glyph: '●' },
        { id: 'themes', label: 'Themes', glyph: 'T' }
      ]
    },
    {
      label: 'Operations',
      links: [
        { id: 'usage', label: 'Usage', glyph: 'U' },
        { id: 'processing', label: 'Processing', glyph: 'P' }
      ]
    },
    {
      label: 'Collaboration',
      links: [{ id: 'organizations', label: 'Organizations', glyph: 'O' }]
    }
  ];

  const initial = (me.label ?? '').slice(0, 1).toUpperCase() || '?';
</script>

<header class="app-mobile-bar">
  <a href="/" class="app-brand">
    <img src="/favicon.svg" alt="" />
    <span><strong>aizk</strong><small>AI Zettelkasten</small></span>
  </a>
  <details>
    <summary>Menu</summary>
    <nav aria-label="Mobile application navigation">
      {#each sections as section}
        <p>{section.label}</p>
        {#each section.links as link}
          <button class:active={view === link.id} onclick={() => navigate(link.id)}>
            <span>{link.glyph}</span>{link.label}
          </button>
        {/each}
      {/each}
      <button class="sign-out" onclick={signOut}>Sign out</button>
    </nav>
  </details>
</header>

<aside class="app-sidebar" aria-label="Primary application navigation">
  <a href="/" class="app-brand">
    <img src="/favicon.svg" alt="" />
    <span><strong>aizk</strong><small>AI Zettelkasten</small></span>
  </a>
  <nav>
    {#each sections as section}
      <section>
        <p>{section.label}</p>
        {#each section.links as link}
          <button
            class:active={view === link.id}
            aria-current={view === link.id ? 'page' : undefined}
            onclick={() => navigate(link.id)}
          >
            <span>{link.glyph}</span>{link.label}
          </button>
        {/each}
      </section>
    {/each}
  </nav>
  <div class="app-account">
    <span class="app-avatar">{initial}</span>
    <span class="app-user"><strong>{me.label ?? 'AIZK user'}</strong><small>Signed in</small></span>
    <button onclick={signOut} title="Sign out" aria-label="Sign out">↗</button>
  </div>
</aside>

<style>
  .app-sidebar { position: fixed; inset: 0 auto 0 0; z-index: 20; display: flex; width: 16rem; flex-direction: column; border-right: 1px solid #dfd1b8; background: #f4e8d0; color: #17223b; }
  .app-brand { display: flex; align-items: center; gap: .7rem; padding: 1.25rem 1.2rem 1rem; color: inherit; text-decoration: none; }
  .app-brand img { width: 2rem; height: 2rem; border-radius: .55rem; }
  .app-brand span, .app-user { display: flex; min-width: 0; flex-direction: column; }
  .app-brand strong { font-family: var(--font-brand); font-size: 1.05rem; letter-spacing: -.03em; }
  .app-brand small, .app-user small { color: #7b8498; font-size: .62rem; letter-spacing: .04em; }
  nav { flex: 1; overflow-y: auto; padding: .4rem .7rem; }
  nav section { margin-bottom: 1.25rem; }
  nav p { margin: 0 0 .3rem; padding: 0 .65rem; color: #9299a8; font-size: .62rem; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; }
  nav button { display: flex; width: 100%; align-items: center; gap: .65rem; border: 0; border-radius: .55rem; background: transparent; padding: .52rem .65rem; color: #596275; font: inherit; font-size: .81rem; text-align: left; cursor: pointer; transition: background .16s ease, color .16s ease; }
  nav button:hover { background: #fffaf180; color: #17223b; }
  nav button.active { background: #fffaf1; color: #315dff; font-weight: 700; box-shadow: inset 0 0 0 1px #dfd1b8; }
  nav button > span { display: grid; width: 1.25rem; height: 1.25rem; place-items: center; border: 1px solid currentColor; border-radius: .36rem; font-family: var(--font-mono); font-size: .58rem; opacity: .72; }
  .app-account { display: flex; align-items: center; gap: .65rem; border-top: 1px solid #dfd1b8; padding: .85rem 1rem; }
  .app-avatar { display: grid; width: 2rem; height: 2rem; flex: none; place-items: center; border-radius: 50%; background: #315dff; color: white; font-size: .75rem; font-weight: 700; }
  .app-user { flex: 1; overflow: hidden; }
  .app-user strong { overflow: hidden; font-size: .76rem; text-overflow: ellipsis; white-space: nowrap; }
  .app-account button { border: 0; background: none; color: #7b8498; cursor: pointer; }
  .app-mobile-bar { display: none; }
  @media (max-width: 767px) {
    .app-sidebar { display: none; }
    .app-mobile-bar { position: sticky; top: 0; z-index: 30; display: flex; height: 4rem; align-items: center; justify-content: space-between; border-bottom: 1px solid #dfd1b8; background: #f4e8d0f2; padding-right: 1rem; backdrop-filter: blur(18px); }
    .app-mobile-bar .app-brand { padding: .65rem 1rem; }
    .app-mobile-bar summary { list-style: none; border: 1px solid #dfe3eb; border-radius: .55rem; padding: .5rem .7rem; color: #4d5668; font-size: .78rem; cursor: pointer; }
    .app-mobile-bar nav { position: absolute; top: 3.65rem; right: 1rem; width: min(20rem, calc(100vw - 2rem)); max-height: 75vh; border: 1px solid #dfe3eb; border-radius: .8rem; background: white; padding: .8rem; box-shadow: 0 18px 50px #1d294327; }
    .app-mobile-bar nav p { margin-top: .8rem; }
    .app-mobile-bar .sign-out { margin-top: .6rem; color: #b3354b; }
  }
</style>
