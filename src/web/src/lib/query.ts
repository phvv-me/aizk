/** Read one URL search parameter as a trimmed value, or `undefined` when blank or absent. */
export function stringParam(value: string | null): string | undefined {
  return value?.trim() ? value.trim() : undefined;
}

/** Read one URL search parameter as a member of `allowed`, or `undefined` otherwise. */
export function enumParam<Allowed extends string>(
  value: string | null,
  allowed: readonly Allowed[]
): Allowed | undefined {
  return value && (allowed as readonly string[]).includes(value) ? (value as Allowed) : undefined;
}
