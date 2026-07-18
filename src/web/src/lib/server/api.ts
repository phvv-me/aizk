import type { LogtoClient } from '@logto/sveltekit';
import { fail, type ActionFailure } from '@sveltejs/kit';
import type {
  Answer,
  Me,
  OrganizationDirectory,
  Overview,
  UploadGrant,
  WriteReceipt
} from '$lib/api';
import * as sdk from '$lib/api/generated';
import { createClient } from '$lib/api/generated/client';
import { settings } from './settings';

/** A rejected browser API call carrying the status and human-facing detail. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    detail: string
  ) {
    super(detail);
  }
}

/** Convert one thrown API call into a form action failure the page can render. */
export function failure(error: unknown): ActionFailure<{ message: string }> {
  if (error instanceof ApiError) return fail(error.status, { message: error.message });
  return fail(502, { message: 'The AIZK API is unreachable right now. Please try again.' });
}

/** Unwrap one completed SDK call into its data, raising rejections as `ApiError`. */
function unwrap<T>({
  data,
  error,
  response
}: {
  data?: T;
  error?: unknown;
  response?: Response;
}): T {
  if (error === undefined) return data as T;
  const status = response?.status ?? 502;
  const body = error as { detail?: unknown; message?: unknown };
  const detail =
    typeof body.detail === 'string'
      ? body.detail
      : typeof body.message === 'string'
        ? body.message
        : `the api answered ${status}`;
  throw new ApiError(status, detail);
}

/** Server-side client binding the generated SDK to the API base URL and the caller's token. */
export class ApiClient {
  constructor(private readonly logtoClient: LogtoClient) {}

  /** One per-call SDK client carrying the caller's fresh Logto access token. */
  private async client() {
    const token = await this.logtoClient.getAccessToken(settings.apiResource);
    return createClient({
      baseUrl: settings.apiUrl,
      headers: { authorization: `Bearer ${token}` }
    });
  }

  async me(): Promise<Me> {
    return unwrap(await sdk.me({ client: await this.client() }));
  }

  async overview(): Promise<Overview> {
    return unwrap(await sdk.overview({ client: await this.client() }));
  }

  async recall(query: string): Promise<Answer> {
    return unwrap(await sdk.recall({ client: await this.client(), body: { query } }));
  }

  async remember(input: {
    text?: string;
    source_uri?: string;
    preserve_source?: boolean;
  }): Promise<WriteReceipt> {
    return unwrap(await sdk.remember({ client: await this.client(), body: input }));
  }

  /** Mint a single-use capability for one declared file; the browser PUTs the bytes itself. */
  async grantUpload(input: {
    filename: string;
    media_type: string;
    size: number;
    companion_text?: string;
  }): Promise<UploadGrant> {
    return unwrap(await sdk.requestUpload({ client: await this.client(), body: input }));
  }

  async organizations(): Promise<OrganizationDirectory> {
    return unwrap(await sdk.organizations({ client: await this.client() }));
  }

  async createOrganization(name: string, description: string): Promise<void> {
    unwrap(
      await sdk.createOrganization({ client: await this.client(), body: { name, description } })
    );
  }

  async addMember(organization: string, email: string, role: string): Promise<void> {
    unwrap(
      await sdk.addMember({
        client: await this.client(),
        path: { name: organization },
        body: { email, role }
      })
    );
  }

  async setMemberRole(organization: string, memberId: string, role: string): Promise<void> {
    unwrap(
      await sdk.setMemberRole({
        client: await this.client(),
        path: { name: organization, member_id: memberId },
        body: { role }
      })
    );
  }

  async removeMember(organization: string, memberId: string): Promise<void> {
    unwrap(
      await sdk.removeMember({
        client: await this.client(),
        path: { name: organization, member_id: memberId }
      })
    );
  }
}
