/// <reference types="vitest/config" />
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  optimizeDeps: {
    exclude: ['@lucide/svelte', 'bits-ui', 'svelte-sonner']
  },
  test: {
    include: ['tests/**/*.test.ts']
  }
});
