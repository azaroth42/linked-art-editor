/**
 * Permission names, exactly as in spec §8.2 and `lae.auth.permissions.ALL_PERMISSIONS`
 * (conventions §2). The UI only uses them to hide controls the user cannot use; the backend
 * checks every request and remains the authority.
 */
export const PERMISSIONS = [
  "edit_draft",
  "publish",
  "delete",
  "merge",
  "unmerge",
  "protect",
  "propose_merge",
  "review_proposal",
  "approve_merge_scoped",
  "manage_vocabularies",
  "manage_roles",
  "manage_users",
  "manage_backups",
  "manage_system",
  "view_analytics",
] as const;

export type Permission = (typeof PERMISSIONS)[number];

/** Whether a user holding `granted` has every one of `required`. */
export function hasPermissions(
  granted: readonly string[] | undefined,
  required: readonly Permission[],
): boolean {
  return granted !== undefined && required.every((permission) => granted.includes(permission));
}
