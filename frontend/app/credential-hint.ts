import type { LlmCredentialMetadata } from "./api";

export function storedKeyPlaceholder(
  credential: Pick<LlmCredentialMetadata, "masked_suffix"> | null,
): string | undefined {
  if (!credential) return undefined;
  // Only the Server's four-character hint is displayable, including on older installations.
  const suffix = /^••••(.{4})$/u.exec(credential.masked_suffix ?? "");
  return suffix ? `********${suffix[1]}` : "********";
}
