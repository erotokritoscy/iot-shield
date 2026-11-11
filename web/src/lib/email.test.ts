import { describe, expect, it } from "vitest";

import { isEmail, splitRecipients, usernameProblem } from "@/lib/email";

describe("isEmail — the server's rule (accounts.validate_email)", () => {
  it.each(["alice@example.com", " Alice@Example.COM ", "first.last+tag@mail.example.org", "admin@admin.com"])(
    "accepts %s",
    (value) => expect(isEmail(value)).toBe(true),
  );

  it.each([
    "alice",
    "alice@",
    "@example.com",
    "alice@example",
    "alice@example.c",
    "al ice@example.com",
    "alice..b@example.com",
    ".alice@example.com",
    "alice.@example.com",
    "alice@-example.com",
  ])("refuses %s", (value) => expect(isEmail(value)).toBe(false));

  it("holds a username to the 64-character column", () => {
    const long = `${"a".repeat(60)}@example.com`;
    expect(isEmail(long)).toBe(true);
    expect(usernameProblem(long)).toMatch(/at most 64 characters/);
  });
});

describe("usernameProblem", () => {
  it("asks for an address, then for a valid one", () => {
    expect(usernameProblem("  ")).toBe("Enter an email address.");
    expect(usernameProblem("alice")).toMatch(/must be a valid email address/);
    expect(usernameProblem("alice@example.com")).toBeNull();
  });
});

describe("splitRecipients", () => {
  it("splits on commas, semicolons and whitespace and drops the blanks", () => {
    expect(splitRecipients(" a@x.io, b@x.io;\nc@x.io ,, ")).toEqual(["a@x.io", "b@x.io", "c@x.io"]);
    expect(splitRecipients("")).toEqual([]);
  });
});
