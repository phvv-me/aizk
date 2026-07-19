import { fail } from '@sveltejs/kit';
import { ApiClient, failure } from '$lib/server/api';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { overview: await new ApiClient(locals.logtoClient).overview() };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { overview: null };
  }
};

export const actions: Actions = {
  // Only the declaration passes through this server. The browser PUTs the raw bytes to the
  // returned same-origin capability path, which Caddy routes straight to the api service, so
  // file content never hairpins through the SvelteKit process or the public tunnel.
  grant: async ({ request, locals }) => {
    const data = await request.formData();
    const filename = String(data.get('filename') ?? '').trim();
    const size = Number(data.get('size'));
    const sha256 = String(data.get('sha256') ?? '').trim();
    if (!filename || !Number.isInteger(size) || size <= 0 || !/^[0-9a-f]{64}$/.test(sha256)) {
      return fail(400, { message: 'Choose a non-empty file to upload.' });
    }
    try {
      const grant = await new ApiClient(locals.logtoClient).grantUpload({
        filename,
        media_type: String(data.get('media_type') ?? '').trim() || 'application/octet-stream',
        size,
        sha256
      });
      // The API advertises an absolute URL; only its path is same-origin for the browser.
      const url = new URL(grant.url);
      return { path: url.pathname + url.search };
    } catch (error) {
      return failure(error);
    }
  },
  intake: async ({ request, locals }) => {
    const data = await request.formData();
    const uri = String(data.get('source_uri') ?? '').trim();
    if (!uri.startsWith('https://')) {
      return fail(400, { message: 'Enter an https:// link to a document or page.' });
    }
    try {
      await new ApiClient(locals.logtoClient).remember({ source_uri: uri, preserve_source: true });
      return { accepted: uri };
    } catch (error) {
      return failure(error);
    }
  }
};
