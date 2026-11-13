export type ParseResponseOptions = {
  redirectOnUnauthorized?: boolean;
};

export const PRESERVE_UNAUTHORIZED_RESPONSE = Object.freeze({
  redirectOnUnauthorized: false,
});

type BrowserNavigation = {
  pathname: string;
  replace(path: string): void;
};

export function applyUnauthorizedNavigation(
  status: number,
  options: ParseResponseOptions,
  navigation: BrowserNavigation,
): boolean {
  if (
    status !== 401
    || options.redirectOnUnauthorized === false
    || navigation.pathname === "/login"
    || navigation.pathname === "/setup"
  ) {
    return false;
  }
  navigation.replace("/login");
  return true;
}
