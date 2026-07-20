<script lang="ts">
  import { enhance } from '$app/forms';
  import { Plus, Trash2, UserPlus } from '@lucide/svelte';
  import { memberRoles } from '$lib/api';
  import InfoTip from '$lib/components/InfoTip.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';
  import { Input } from '$lib/components/ui/input';
  import { Label } from '$lib/components/ui/label';
  import { feedback } from '$lib/forms';
  import type { PageServerData } from './$types';

  let { data }: { data: PageServerData } = $props();
  let creating = $state(false);

  const selectClass =
    'border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-8 rounded-md border bg-transparent px-2 text-sm outline-none focus-visible:ring-[3px]';
</script>

<PageHeader
  title="Organizations"
  description="Review collaboration scopes and manage membership when your Logto role permits it."
/>

<Card.Root class="mb-8">
  <Card.Header>
    <div class="flex items-center gap-2">
      <Card.Title>Create an organization</Card.Title>
      <InfoTip
        label="What an organization does"
        text="An organization is a Logto-owned collaboration scope. Membership grants shared recall, while viewer, editor, and admin roles control collaboration permissions."
      />
    </div>
    <Card.Description>Start a shared memory space and manage its members here.</Card.Description>
  </Card.Header>
  <Card.Content>
    <form
      method="POST"
      action="?/create"
      class="flex flex-wrap items-end gap-3"
      use:enhance={feedback('Organization created.', { pending: (active) => (creating = active) })}
    >
      <div class="min-w-48 flex-1 space-y-2">
        <Label for="organization-name">Name</Label>
        <Input id="organization-name" name="name" required placeholder="acme" />
      </div>
      <div class="min-w-64 flex-[2] space-y-2">
        <Label for="organization-description">Description</Label>
        <Input id="organization-description" name="description" placeholder="What it is for" />
      </div>
      <Button type="submit" disabled={creating}>
        <Plus aria-hidden="true" />
        Create
      </Button>
    </form>
  </Card.Content>
</Card.Root>

{#if !data.directory}
  <Card.Root>
    <Card.Header>
      <Card.Title>Directory unavailable</Card.Title>
      <Card.Description>
        Your memberships will appear once the AIZK API answers again.
      </Card.Description>
    </Card.Header>
  </Card.Root>
{:else if data.directory.organizations.length === 0}
  <Card.Root>
    <Card.Header>
      <Card.Title>No memberships yet</Card.Title>
      <Card.Description>
        Create an organization above or ask an admin to invite you.
      </Card.Description>
    </Card.Header>
  </Card.Root>
{:else}
  <div class="space-y-6">
    {#each data.directory.organizations as organization (organization.name)}
      <Card.Root id={organization.name}>
        <Card.Header>
          <Card.Title>{organization.name}</Card.Title>
          {#if organization.description}
            <Card.Description>{organization.description}</Card.Description>
          {/if}
          <Card.Action>
            <span class="flex flex-wrap gap-1" aria-label="Your roles in {organization.name}">
              {#each organization.roles as role (role)}
                <Badge variant="secondary">{role}</Badge>
              {/each}
            </span>
          </Card.Action>
        </Card.Header>
        {#if organization.can_manage_members || organization.can_delete_members}
          <Card.Content class="space-y-4">
            <ul class="divide-border divide-y" aria-label="Members of {organization.name}">
              {#each organization.members as member (member.id)}
                <li class="flex flex-wrap items-center gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0">
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-medium">{member.label}</p>
                    <p class="text-muted-foreground text-xs">{member.roles.join(', ')}</p>
                  </div>
                  {#if organization.can_manage_members}
                    <form method="POST" action="?/role" use:enhance={feedback('Role changed.')}>
                      <input type="hidden" name="organization" value={organization.name} />
                      <input type="hidden" name="member" value={member.id} />
                      <select
                        name="role"
                        class={selectClass}
                        aria-label="Role of {member.label}"
                        value={member.roles[0] ?? 'viewer'}
                        onchange={(event) => event.currentTarget.form?.requestSubmit()}
                      >
                        {#each memberRoles as role (role)}
                          <option value={role}>{role}</option>
                        {/each}
                      </select>
                    </form>
                  {/if}
                  {#if organization.can_delete_members}
                    <form method="POST" action="?/remove" use:enhance={feedback('Member removed.')}>
                      <input type="hidden" name="organization" value={organization.name} />
                      <input type="hidden" name="member" value={member.id} />
                      <Button
                        type="submit"
                        variant="destructive"
                        size="icon-sm"
                        aria-label="Remove {member.label} from {organization.name}"
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </form>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if organization.can_manage_members}
              <form
                method="POST"
                action="?/add"
                class="flex flex-wrap items-end gap-3"
                use:enhance={feedback('Member invited.')}
              >
                <input type="hidden" name="organization" value={organization.name} />
                <div class="min-w-56 flex-1 space-y-2">
                  <Label for="add-email-{organization.name}">Member email</Label>
                  <Input
                    id="add-email-{organization.name}"
                    name="email"
                    type="email"
                    required
                    placeholder="person@example.com"
                  />
                </div>
                <div class="space-y-2">
                  <Label for="add-role-{organization.name}">Role</Label>
                  <select id="add-role-{organization.name}" name="role" class={selectClass}>
                    {#each memberRoles as role (role)}
                      <option value={role}>{role}</option>
                    {/each}
                  </select>
                </div>
                <Button type="submit" variant="secondary">
                  <UserPlus aria-hidden="true" />
                  Add member
                </Button>
              </form>
            {/if}
          </Card.Content>
        {/if}
      </Card.Root>
    {/each}
  </div>
{/if}
