import type { RequestHandler } from './$types';

// The container healthcheck. Every other route either needs a session or belongs to the static
// site, so this is the one path that proves the render server itself is answering.
export const GET: RequestHandler = () => new Response('ok');
