import { redirect } from '@sveltejs/kit';
import { adminRoutes } from '$lib/routes';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async () => {
  redirect(302, adminRoutes.overview);
};
