<script lang="ts">
  import { CircleHelp } from '@lucide/svelte';

  let { label, text }: { label: string; text: string } = $props();
  let trigger: HTMLButtonElement;
  let tooltip: HTMLSpanElement;

  function place(): void {
    const triggerBox = trigger.getBoundingClientRect();
    const tooltipBox = tooltip.getBoundingClientRect();
    const gutter = 8;
    const left = Math.min(
      Math.max(triggerBox.right - tooltipBox.width, gutter),
      window.innerWidth - tooltipBox.width - gutter
    );
    const below = triggerBox.bottom + gutter;
    const top =
      below + tooltipBox.height <= window.innerHeight - gutter
        ? below
        : Math.max(gutter, triggerBox.top - tooltipBox.height - gutter);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function show(): void {
    if (!tooltip.matches(':popover-open')) {
      tooltip.showPopover();
    }
    place();
  }

  function hide(): void {
    if (tooltip.matches(':popover-open')) {
      tooltip.hidePopover();
    }
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      hide();
    }
  }
</script>

<span class="inline-flex align-middle">
  <button
    bind:this={trigger}
    type="button"
    class="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex cursor-help rounded-full focus-visible:ring-2 focus-visible:outline-none"
    aria-label={label}
    onpointerenter={show}
    onpointerleave={hide}
    onfocus={show}
    onblur={hide}
    onkeydown={handleKeydown}
  >
    <CircleHelp class="size-4" aria-hidden="true" />
  </button>
  <span
    bind:this={tooltip}
    popover="manual"
    class="border-border bg-popover text-popover-foreground fixed inset-auto m-0 w-72 max-w-[calc(100vw-1rem)] rounded-md border px-3 py-2 text-left text-xs leading-relaxed shadow-xl"
    role="tooltip"
  >
    {text}
  </span>
</span>
