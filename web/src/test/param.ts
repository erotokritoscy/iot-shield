import type { ParamInfo } from "@/lib/api/types";

/**
 * One adapter param as `/modules` and `/eval/detectors` report it.
 *
 * One factory rather than a copy per suite, and the `: ParamInfo` return annotation is the
 * point of sharing it: `ParamInfo` is generated from the APIs' `/openapi.json`
 * (`npm run gen:types`), so this annotation is the only thing that makes a hand-built fixture
 * fail loudly when the real param shape drifts. There were three copies and the newest had
 * dropped it, which would have left that suite green while asserting over a shape the API
 * cannot produce.
 */
export const param = (
  name: string,
  type: string | null,
  value: unknown,
  choices: string[] | null = null,
): ParamInfo => ({ name, type, default: value, required: false, choices });
