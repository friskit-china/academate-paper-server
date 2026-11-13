export type BrowserLocationView = Pick<Location, "hash" | "pathname" | "search">;
export type BrowserHistoryView = Pick<History, "replaceState">;

export function consumeLaunchTokenFragment(
  location: BrowserLocationView,
  history: BrowserHistoryView,
): string | null {
  const fragment = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  const rawToken = new URLSearchParams(fragment).get("token");
  const cleanSearch = new URLSearchParams(location.search);
  cleanSearch.delete("token");
  cleanSearch.delete("launch_token");
  const serializedSearch = cleanSearch.toString();

  // Clear the fragment before any network operation so the one-use proof does not survive in
  // browser history, copied URLs, referrers, screenshots, or a later navigation.
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

export async function exchangeLocalLaunchToken(
  rawToken: string,
  fetchImplementation: typeof fetch = fetch,
): Promise<void> {
  const response = await fetchImplementation("/api/v1/local-session/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ launch_token: rawToken }),
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error(
      response.status === 409
        ? "This browser launch proof has already been used. Reopen AcadeMate Paper to continue."
        : response.status === 410
          ? "This browser launch proof expired. Reopen AcadeMate Paper to continue."
          : "The browser launch could not be verified. Reopen AcadeMate Paper to continue.",
    );
  }
}
