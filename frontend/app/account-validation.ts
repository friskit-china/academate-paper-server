const encoder = new TextEncoder();

export function validateUsername(value: string): string | null {
  const normalized = value.trim().normalize("NFKC").toLocaleLowerCase();
  if (normalized.length < 3 || normalized.length > 64 || encoder.encode(normalized).length > 256) {
    return "用户名需要 3 至 64 个字符。";
  }
  if (!/^[\p{L}\p{N}][\p{L}\p{N}._-]*$/u.test(normalized)) {
    return "用户名需要以字母或数字开头，且只能包含字母、数字、点、下划线或连字符。";
  }
  return null;
}

export function validateDisplayName(value: string): string | null {
  const normalized = value.trim();
  if (
    normalized.length < 1
    || normalized.length > 128
    || encoder.encode(normalized).length > 512
    || /[\0\r\n]/.test(normalized)
  ) {
    return "显示名称需要是 1 至 128 个字符的单行文字。";
  }
  return null;
}

export function validatePassword(value: string): string | null {
  if (
    value.length < 12
    || value.length > 1024
    || encoder.encode(value).length > 4096
    || value.includes("\0")
  ) {
    return "密码需要 12 至 1024 个字符，且不能包含空字符。";
  }
  return null;
}
