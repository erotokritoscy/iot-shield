/**
 * Email addresses, as the server judges them (`storage/accounts.py: validate_email`, ADR-0052).
 *
 * A username IS an email address — it is where a job's results are mailed by default — so the
 * forms that create an account check it here. The server is the authority; this only saves a
 * round trip and says why before the user presses the button.
 */

/** Mirrors `accounts.MAX_USERNAME` (the users.username column). */
export const MAX_USERNAME = 64;
/** Mirrors `accounts.MAX_EMAIL` (RFC 5321's limit on a forward path). */
export const MAX_EMAIL = 254;

const EMAIL = /^[a-z0-9](?:[a-z0-9._%+-]*[a-z0-9_%+-])?@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/;

/** True when the (trimmed, case-insensitive) value is an address the server will accept. */
export function isEmail(value: string, maxLength: number = MAX_EMAIL): boolean {
  const address = value.trim().toLowerCase();
  return address.length <= maxLength && !address.includes("..") && EMAIL.test(address);
}

/** The problem with a username, in words, or null when it is a usable one. */
export function usernameProblem(value: string): string | null {
  if (!value.trim()) return "Enter an email address.";
  if (!isEmail(value, MAX_USERNAME)) {
    return `The username must be a valid email address (e.g. name@example.com), at most ${MAX_USERNAME} characters.`;
  }
  return null;
}

/** Split a free-text recipients box (commas, semicolons, spaces, new lines) into addresses. */
export function splitRecipients(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}
