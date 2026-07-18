import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter(),
    // Everything the app needs is same-origin behind Caddy. https: in img-src keeps
    // remote images inside recalled Markdown rendering while scripts stay nonce-gated.
    csp: {
      mode: 'auto',
      directives: {
        'default-src': ["'self'"],
        'script-src': ["'self'"],
        'style-src': ["'self'", "'unsafe-inline'"],
        'img-src': ["'self'", 'data:', 'https:'],
        'connect-src': ["'self'"],
        'font-src': ["'self'", 'data:'],
        'object-src': ["'none'"],
        'base-uri': ["'self'"],
        'frame-ancestors': ["'none'"]
      }
    }
  }
};

export default config;
