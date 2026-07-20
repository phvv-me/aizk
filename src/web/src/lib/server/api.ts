import type { LogtoClient } from '@logto/sveltekit';
import { fail, type ActionFailure } from '@sveltejs/kit';
import type {
  Answer,
  FindingPage,
  GraphSlice,
  Me,
  OrganizationDirectory,
  Overview,
  ProcessingReport,
  SourcePage,
  SubjectPage,
  ThemePage,
  UsageReport
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

  async usage(days = 30): Promise<UsageReport> {
    return unwrap(await sdk.usage({ client: await this.client(), query: { days } }));
  }

  async processing(): Promise<ProcessingReport> {
    return unwrap(await sdk.processing({ client: await this.client() }));
  }

  async processingEvents(signal: AbortSignal): Promise<Response> {
    const token = await this.logtoClient.getAccessToken(settings.apiResource);
    return fetch(`${settings.apiUrl}/api/processing/events`, {
      headers: { authorization: `Bearer ${token}` },
      signal
    });
  }

  async sources(search = '', limit = 50, offset = 0): Promise<SourcePage> {
    return unwrap(
      await sdk.sources({ client: await this.client(), query: { search, limit, offset } })
    );
  }

  async findings(search = '', limit = 50, offset = 0): Promise<FindingPage> {
    return unwrap(
      await sdk.findings({ client: await this.client(), query: { search, limit, offset } })
    );
  }

  async subjects(search = '', limit = 50, offset = 0): Promise<SubjectPage> {
    return unwrap(
      await sdk.subjects({ client: await this.client(), query: { search, limit, offset } })
    );
  }

  async themes(): Promise<ThemePage> {
    return unwrap(await sdk.themes({ client: await this.client() }));
  }

  async graph(limit = 40): Promise<GraphSlice> {
    return unwrap(await sdk.graph({ client: await this.client(), query: { limit } }));
  }

  async recall(query: string): Promise<Answer> {
    return unwrap(await sdk.recall({ client: await this.client(), body: { query } }));
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
