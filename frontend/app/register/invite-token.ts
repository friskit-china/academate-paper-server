export type RegistrationLocationView = Pick<Location, "hash" | "pathname" | "search">;
export type RegistrationHistoryView = Pick<History, "replaceState">;

export function consumeInvitationTokenFragment(
  location: RegistrationLocationView,
  history: RegistrationHistoryView,
): string | null {
  const fragment = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const fragmentParameters = new URLSearchParams(fragment);
  const candidates = fragmentParameters.getAll("invite");
  const cleanSearch = new URLSearchParams(location.search);
  cleanSearch.delete("invite");
  cleanSearch.delete("invite_token");
  const serializedSearch = cleanSearch.toString();

  // Clear every invite-bearing URL component before validation or network work. The accepted proof
  // is returned to the caller and lives only in the current component's memory.
  history.replaceState(
    null,
    "",
    `${location.pathname}${serializedSearch.length === 0 ? "" : `?${serializedSearch}`}`,
  );
  if (candidates.length !== 1) return null;
  const rawToken = candidates[0];
  if (
    rawToken.length < 32
    || rawToken.length > 512
    || !/^[\x21-\x7e]+$/.test(rawToken)
  ) return null;
  return rawToken;
}
