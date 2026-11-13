import {
  ApiRequestError,
  createAutomaticOwnerSession,
  getWebSessionStatus,
} from "../api.ts";

export type AutomaticOwnerSessionOutcome = "authenticated" | "cookie-rejected";

export type AutomaticOwnerSessionOperations = Readonly<{
  checkSession(): Promise<unknown>;
  createSession(): Promise<unknown>;
}>;

const defaultOperations: AutomaticOwnerSessionOperations = {
  checkSession: getWebSessionStatus,
  createSession: createAutomaticOwnerSession,
};

function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 401;
}

/**
 * Reuse an existing session when possible, and create one only after an explicit 401. The second
 * status request proves that the browser accepted the HttpOnly Set-Cookie instruction; callers
 * must stop instead of retrying automatically when cookies are disabled.
 */
export async function establishAutomaticOwnerSession(
  operations: AutomaticOwnerSessionOperations = defaultOperations,
): Promise<AutomaticOwnerSessionOutcome> {
  try {
    await operations.checkSession();
    return "authenticated";
  } catch (error) {
    if (!isUnauthorized(error)) throw error;
  }

  await operations.createSession();
  try {
    await operations.checkSession();
    return "authenticated";
  } catch (error) {
    if (isUnauthorized(error)) return "cookie-rejected";
    throw error;
  }
}
