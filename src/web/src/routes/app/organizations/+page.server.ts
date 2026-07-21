import { fail } from '@sveltejs/kit';
import { ApiClient, failure } from '$lib/server/api';
import { memberRoles } from '$lib/api';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { directory: await new ApiClient(locals.logtoClient).organizations() };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { directory: null };
  }
};

function field(data: FormData, name: string): string {
  return String(data.get(name) ?? '').trim();
}

function validRole(value: string): boolean {
  return memberRoles.includes(value as (typeof memberRoles)[number]);
}

export const actions: Actions = {
  create: async ({ request, locals }) => {
    const data = await request.formData();
    const name = field(data, 'name');
    if (!name) return fail(400, { message: 'Name the organization first.' });
    try {
      await new ApiClient(locals.logtoClient).createOrganization(name, field(data, 'description'));
      return { created: name };
    } catch (error) {
      return failure(error);
    }
  },
  add: async ({ request, locals }) => {
    const data = await request.formData();
    const email = field(data, 'email');
    const role = field(data, 'role');
    if (!email) return fail(400, { message: 'Enter the new member email.' });
    if (!validRole(role))
      return fail(400, { message: `Pick a role among ${memberRoles.join(', ')}.` });
    try {
      await new ApiClient(locals.logtoClient).addMember(field(data, 'organization'), email, role);
      return { added: email };
    } catch (error) {
      return failure(error);
    }
  },
  role: async ({ request, locals }) => {
    const data = await request.formData();
    const role = field(data, 'role');
    if (!validRole(role))
      return fail(400, { message: `Pick a role among ${memberRoles.join(', ')}.` });
    try {
      await new ApiClient(locals.logtoClient).setMemberRole(
        field(data, 'organization'),
        field(data, 'member'),
        role
      );
      return { changed: field(data, 'member') };
    } catch (error) {
      return failure(error);
    }
  },
  remove: async ({ request, locals }) => {
    const data = await request.formData();
    try {
      await new ApiClient(locals.logtoClient).removeMember(
        field(data, 'organization'),
        field(data, 'member')
      );
      return { removed: field(data, 'member') };
    } catch (error) {
      return failure(error);
    }
  }
};
