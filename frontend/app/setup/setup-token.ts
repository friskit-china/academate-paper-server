export type SetupLocationView = Pick<Location, "hash" | "pathname" | "search">;
export type SetupHistoryView = Pick<History, "replaceState">;

export function consumeSetupTokenFragment(
  location: SetupLocationView,
  history: SetupHistoryView,
): string | null {
  const fragment = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const rawToken = new URLSearchParams(fragment).get("token");
  const cleanSearch = new URLSearchParams(location.search);
  cleanSearch.delete("token");
  cleanSearch.delete("setup_token");
  const serializedSearch = cleanSearch.toString();

  // Setup proof delivery follows the launch-proof boundary: consume only a fragment, then erase it
  // before the user types a password or any request is made. The proof remains React state only.
  history.replaceState(
    null,
    "",
    `${location.pathname}${serializedSearch.length === 0 ? "" : `?${serializedSearch}`}`,
  );
  if (
    rawToken === null
    || rawToken.length < 32
    || rawToken.length > 512
    || !/^[\x21-\x7e]+$/.test(rawToken)
  ) {
    return null;
  }
  return rawToken;
}
