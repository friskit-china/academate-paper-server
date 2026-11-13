export type WebSessionUser = Readonly<{
  id: string;
  username: string;
  display_name: string;
}>;

export type WebSessionWorkspace = Readonly<{
  id: string;
  name: string;
}>;

export type WebSessionStatus = Readonly<{
  authenticated: true;
  session_id?: string;
  expires_at?: string;
  user_id: string;
  active_workspace_id: string;
  user: WebSessionUser;
  workspace: WebSessionWorkspace;
  onboarding_required: boolean;
  can_manage_users: boolean;
  can_manage_invitations: boolean;
}>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

export function parseWebSessionStatus(value: unknown): WebSessionStatus | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "active_workspace_id",
    "authenticated",
    "can_manage_invitations",
    "can_manage_users",
    "onboarding_required",
    "user",
    "user_id",
    "workspace",
    ...("session_id" in value ? ["session_id"] : []),
    ...("expires_at" in value ? ["expires_at"] : []),
  ])) return null;
  if ("session_id" in value && !nonEmptyString(value.session_id)) return null;
  if ("expires_at" in value && (typeof value.expires_at !== "string"
    || !Number.isFinite(Date.parse(value.expires_at)))) return null;
  if (!isRecord(value.user) || !hasExactKeys(value.user, [
    "display_name",
    "id",
    "username",
  ])) return null;
  if (!isRecord(value.workspace) || !hasExactKeys(value.workspace, ["id", "name"])) return null;
  if (
    value.authenticated !== true
    || !nonEmptyString(value.user_id)
    || !nonEmptyString(value.active_workspace_id)
    || !nonEmptyString(value.user.id)
    || !nonEmptyString(value.user.username)
    || typeof value.user.display_name !== "string"
    || !nonEmptyString(value.workspace.id)
    || typeof value.workspace.name !== "string"
    || value.user.id !== value.user_id
    || value.workspace.id !== value.active_workspace_id
    || typeof value.onboarding_required !== "boolean"
    || typeof value.can_manage_users !== "boolean"
    || typeof value.can_manage_invitations !== "boolean"
  ) return null;

  return Object.freeze({
    authenticated: true,
    ...("session_id" in value ? { session_id: value.session_id as string } : {}),
    ...("expires_at" in value ? { expires_at: value.expires_at as string } : {}),
    user_id: value.user_id,
    active_workspace_id: value.active_workspace_id,
    user: Object.freeze({
      id: value.user.id,
      username: value.user.username,
      display_name: value.user.display_name,
    }),
    workspace: Object.freeze({ id: value.workspace.id, name: value.workspace.name }),
    onboarding_required: value.onboarding_required,
    can_manage_users: value.can_manage_users,
    can_manage_invitations: value.can_manage_invitations,
  });
}

export function authenticatedLandingPath(
  status: WebSessionStatus,
): "/onboarding" | "/library" {
  return status.onboarding_required ? "/onboarding" : "/library";
}
