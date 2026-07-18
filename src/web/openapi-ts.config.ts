import { defineConfig } from '@hey-api/openapi-ts';

/** Regenerate the typed API client from the schema `aizk openapi` dumps next to this file. */
export default defineConfig({
  input: 'openapi.json',
  output: 'src/lib/api/generated',
  plugins: ['@hey-api/client-fetch', '@hey-api/typescript', '@hey-api/sdk']
});
