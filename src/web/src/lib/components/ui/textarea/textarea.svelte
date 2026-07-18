<script lang="ts">
	import type { HTMLTextareaAttributes } from "svelte/elements";
	import { cn, type WithElementRef } from "$lib/utils.js";

	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		...restProps
	}: WithElementRef<HTMLTextareaAttributes> = $props();
</script>

<textarea
	bind:this={ref}
	data-slot="textarea"
	class={cn(
		"border-input placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive dark:bg-input/30 shadow-xs flex field-sizing-content min-h-16 w-full rounded-md border bg-transparent px-3 py-2 text-base outline-none transition-[color,box-shadow] disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
		"focus-visible:ring-[3px]",
		className
	)}
	bind:value
	{...restProps}
></textarea>
