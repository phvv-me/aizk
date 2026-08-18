<script lang="ts">
  import { page } from '$app/state';
  import {
    Activity,
    Building2,
    ChartNoAxesCombined,
    ChevronsUpDown,
    CircleDot,
    Compass,
    ExternalLink,
    FileText,
    HardDrive,
    Inbox,
    LayoutDashboard,
    Lightbulb,
    ListChecks,
    LogOut,
    Menu,
    MessageCircleQuestion,
    Network,
    Settings,
    type Icon as IconType
  } from '@lucide/svelte';
  import type { Me } from '$lib/api';
  import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
  import { Separator } from '$lib/components/ui/separator';
  import type { NavIcon, NavSection } from '$lib/nav';
  import { cn } from '$lib/utils';

  type ExternalToolLink = { label: string; href: string; description: string };

  let {
    me,
    accountUrl,
    sections,
    brandHref,
    externalLinks = []
  }: {
    me: Me;
    accountUrl: string;
    sections: NavSection[];
    brandHref: string;
    externalLinks?: ExternalToolLink[];
  } = $props();

  const icons: Record<NavIcon, typeof IconType> = {
    dashboard: LayoutDashboard,
    find: MessageCircleQuestion,
    explore: Compass,
    sources: FileText,
    findings: Lightbulb,
    subjects: CircleDot,
    themes: Network,
    usage: ChartNoAxesCombined,
    processing: Activity,
    organizations: Building2,
    overview: LayoutDashboard,
    queues: ListChecks,
    ingestion: Inbox,
    storage: HardDrive
  };
  const initial = $derived((me.label ?? '').slice(0, 1).toUpperCase() || '?');
</script>

<header class="border-sidebar-border bg-sidebar sticky top-0 z-30 border-b md:hidden">
  <div class="flex h-14 items-center justify-between px-4">
    <a
      href={brandHref}
      class="flex items-center gap-2 font-semibold tracking-tight"
      style="font-family: var(--font-brand)"
    >
      <img src="/favicon.svg" alt="" class="size-6" />
      aizk
    </a>
    <details class="group relative">
      <summary
        class="hover:bg-accent focus-visible:ring-ring flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-sm focus-visible:ring-2 focus-visible:outline-none"
      >
        <Menu class="size-4" aria-hidden="true" />
        Menu
      </summary>
      <div
        class="border-border bg-popover text-popover-foreground absolute right-0 mt-2 max-h-[75vh] w-72 overflow-y-auto rounded-lg border p-3 shadow-lg"
      >
        <nav class="space-y-4" aria-label="Mobile sections">
          {#each sections as section (section.label)}
            <div>
              <p
                class="text-muted-foreground px-2 pb-1 text-xs font-medium tracking-wide uppercase"
              >
                {section.label}
              </p>
              {#each section.links as link (link.href)}
                {@const Icon = icons[link.icon]}
                <a
                  href={link.href}
                  class="hover:bg-accent flex items-center gap-2 rounded-md px-2 py-2 text-sm"
                >
                  <Icon class="size-4" aria-hidden="true" />
                  {link.label}
                </a>
              {/each}
            </div>
          {/each}
        </nav>
        {#if externalLinks.length > 0}
          <Separator class="my-3" />
          <p class="text-muted-foreground px-2 pb-1 text-xs font-medium tracking-wide uppercase">
            External tools
          </p>
          {#each externalLinks as link (link.href)}
            <a
              href={link.href}
              target="_blank"
              rel="noreferrer"
              class="text-muted-foreground hover:bg-accent hover:text-foreground flex items-center gap-2 rounded-md px-2 py-2 text-sm"
            >
              <ExternalLink class="size-4 shrink-0" aria-hidden="true" />
              {link.label}
            </a>
          {/each}
        {/if}
        <Separator class="my-3" />
        <a
          href={accountUrl}
          class="hover:bg-accent flex items-center gap-2 rounded-md px-2 py-2 text-sm"
        >
          <Settings class="size-4" aria-hidden="true" />
          Account settings
        </a>
        <form method="POST" action="/auth/sign-out">
          <button
            type="submit"
            class="text-destructive hover:bg-accent flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm"
          >
            <LogOut class="size-4" aria-hidden="true" />
            Sign out
          </button>
        </form>
      </div>
    </details>
  </div>
</header>

<aside
  class="border-sidebar-border bg-sidebar text-sidebar-foreground fixed inset-y-0 left-0 z-20 hidden w-64 flex-col border-r md:flex"
  aria-label="Primary"
>
  <a
    href={brandHref}
    class="flex items-center gap-2 px-5 py-5 text-lg font-semibold tracking-tight"
    style="font-family: var(--font-brand)"
  >
    <img src="/favicon.svg" alt="" class="size-7" />
    aizk
  </a>
  <nav class="flex-1 space-y-6 overflow-y-auto px-3 py-2" aria-label="Sections">
    {#each sections as section (section.label)}
      <div>
        <p class="text-muted-foreground px-2 pb-1 text-xs font-medium tracking-wide uppercase">
          {section.label}
        </p>
        <ul class="space-y-0.5">
          {#each section.links as link (link.href)}
            {@const Icon = icons[link.icon]}
            {@const active = page.url.pathname === link.href}
            <li>
              <a
                href={link.href}
                aria-current={active ? 'page' : undefined}
                class={cn(
                  'focus-visible:ring-ring flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none',
                  active
                    ? 'bg-accent text-accent-foreground font-medium'
                    : 'hover:bg-accent/60 hover:text-accent-foreground'
                )}
              >
                <Icon class="size-4 shrink-0" aria-hidden="true" />
                <span class="truncate">{link.label}</span>
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </nav>
  {#if externalLinks.length > 0}
    <div class="border-sidebar-border border-t px-3 py-3">
      <p class="text-muted-foreground px-2 pb-1 text-xs font-medium tracking-wide uppercase">
        External tools
      </p>
      <ul class="space-y-0.5">
        {#each externalLinks as link (link.href)}
          <li>
            <a
              href={link.href}
              target="_blank"
              rel="noreferrer"
              title={link.description}
              class="text-muted-foreground hover:bg-accent/60 hover:text-foreground focus-visible:ring-ring flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none"
            >
              <ExternalLink class="size-4 shrink-0" aria-hidden="true" />
              <span class="flex-1 truncate">{link.label}</span>
            </a>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
  <Separator />
  <div class="p-3">
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        class="hover:bg-accent/60 focus-visible:ring-ring flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left focus-visible:ring-2 focus-visible:outline-none"
        aria-label="Account menu"
      >
        <span
          class="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold"
          aria-hidden="true"
        >
          {initial}
        </span>
        <span class="flex-1 truncate text-sm font-medium">{me.label}</span>
        <ChevronsUpDown class="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
      </DropdownMenu.Trigger>
      <DropdownMenu.Content class="w-56" align="start" side="top">
        <DropdownMenu.Item>
          {#snippet child({ props })}
            <a href={accountUrl} {...props}>
              <Settings class="size-4" aria-hidden="true" />
              Account settings
            </a>
          {/snippet}
        </DropdownMenu.Item>
        <DropdownMenu.Separator />
        <DropdownMenu.Item variant="destructive">
          {#snippet child({ props })}
            <form method="POST" action="/auth/sign-out" class="contents">
              <button type="submit" {...props}>
                <LogOut class="size-4" aria-hidden="true" />
                Sign out
              </button>
            </form>
          {/snippet}
        </DropdownMenu.Item>
      </DropdownMenu.Content>
    </DropdownMenu.Root>
  </div>
</aside>
