import type { RequestHandler } from './$types';
import { ApiClient } from '$lib/server/api';

export const GET: RequestHandler = async ({ locals, request }) => {
  if (!locals.user) {
    return new Response('sign in is required', { status: 401 });
  }

  let upstream: Response;
  try {
    upstream = await new ApiClient(locals.logtoClient).processingEvents(request.signal);
  } catch {
    return new Response('processing updates are unavailable', { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    await upstream.body?.cancel();
    return new Response('processing updates are unavailable', { status: upstream.status || 502 });
  }

  return new Response(upstream.body, {
    headers: {
      'Cache-Control': 'no-cache, no-transform',
      'Content-Type': 'text/event-stream',
      'X-Accel-Buffering': 'no'
    }
  });
};
