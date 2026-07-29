// Regenerate the TypeScript views of both APIs from their OpenAPI documents (D4).
//
// The old dashboard validated every response against the pydantic contracts, so API/UI
// drift failed loudly at one boundary instead of rendering wrong (ADR-0008). TS types are
// erased at runtime and cannot carry that alone — so this half gives us *compile-time*
// agreement with the server's own schema, and `src/lib/api/schemas.ts` gives us the runtime
// half on the load-bearing shapes. Both halves are needed; neither substitutes.
//
// Output is committed, so a clone can build without Python. Run after changing either API:
//   npm run gen:types
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(web, "..");
const openapi = path.join(web, "src", "lib", "api", "openapi");

mkdirSync(openapi, { recursive: true });

const run = (cmd, args, cwd) =>
  execFileSync(cmd, args, { cwd, stdio: "inherit", shell: process.platform === "win32" });

run("uv", ["run", "python", path.join("web", "scripts", "dump_openapi.py"), openapi], repo);

for (const name of ["read", "pipeline"]) {
  run(
    "npx",
    [
      "openapi-typescript",
      path.join(openapi, `${name}.json`),
      "-o",
      path.join(web, "src", "lib", "api", `${name}.gen.ts`),
    ],
    web,
  );
}
