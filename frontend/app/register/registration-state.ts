import {
  validateDisplayName,
  validatePassword,
  validateUsername,
} from "../account-validation.ts";

export type RegistrationDraft = Readonly<{
  username: string;
  displayName: string;
  password: string;
  passwordConfirmation: string;
}>;

export function validateRegistrationDraft(draft: RegistrationDraft): string | null {
  const usernameError = validateUsername(draft.username);
  if (usernameError !== null) return usernameError;
  const displayNameError = validateDisplayName(draft.displayName);
  if (displayNameError !== null) return displayNameError;
  const passwordError = validatePassword(draft.password);
  if (passwordError !== null) return passwordError;
  if (draft.password !== draft.passwordConfirmation) {
    return "两次输入的密码不一致。";
  }
  return null;
}
