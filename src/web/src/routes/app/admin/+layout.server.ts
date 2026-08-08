import { error } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';

/**
 * Refuse the operator pages to anyone the API did not vouch for as an operator.
 *
 * The parent layout already established the session, read the caller and loaded the shell
 * these pages render inside, so this adds only the check that the pages themselves need.
 * `admin` is false whenever the API could not be asked, which is what makes an unreachable
 * API refuse the console rather than degrade it into a shell claiming standing it never
 * proved.
 */
export const load: LayoutServerLoad = async ({ parent }) => {
  const { me } = await parent();
  if (!me.admin) error(403, 'Operator access required.');
  return {};
};
