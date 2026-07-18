<script lang="ts">
  import { page } from '$app/state';
  import {
    Building2,
    ChevronsUpDown,
    FileText,
    LayoutDashboard,
    LogOut,
    MessageCircleQuestion,
    Settings,
    Sparkles,
    Users,
    type Icon as IconType
  } from '@lucide/svelte';
  import type { Me } from '$lib/api';
  import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
  import { Separator } from '$lib/components/ui/separator';
  import { navigation, type NavIcon } from '$lib/nav';
  import { cn } from '$lib/utils';

  let { me, accountUrl }: { me: Me; accountUrl: string } = $props();

  const icons: Record<NavIcon, typeof IconType> = {
    dashboard: LayoutDashboard,
    recall: MessageCircleQuestion,
    sources: FileText,
    organizations: Building2,
    members: Users
  };
  const sections = $derived(navigation(me));
  const initial = $derived((me.label ?? '').slice(0, 1).toUpperCase() || '?');
</script>

<aside
  class="border-sidebar-border bg-sidebar text-sidebar-foreground fixed inset-y-0 left-0 z-20 flex w-64 flex-col border-r"
  aria-label="Primary"
>
  <a
    href="/dashboard"
    class="flex items-center gap-2 px-5 py-5 text-lg font-semibold tracking-tight"
  >
    <Sparkles class="text-primary size-5" aria-hidden="true" />
    AIZK
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
            {@const active = page.url.pathname === link.href.split('#')[0]}
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
