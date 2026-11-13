export function isCanonicalRegistrationUrl(value: string, browserOrigin: string): boolean {
  try {
    const url = new URL(value);
    if (url.origin !== browserOrigin || url.pathname !== "/register" || url.search !== "") {
      return false;
    }
    const fragment = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
    const tokens = fragment.getAll("invite");
    return [...fragment.keys()].every((key) => key === "invite")
      && tokens.length === 1
      && tokens[0].length >= 32
      && tokens[0].length <= 512
      && /^[\x21-\x7e]+$/.test(tokens[0]);
  } catch {
    return false;
  }
}
