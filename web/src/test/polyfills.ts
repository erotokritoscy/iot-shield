// Loaded before every other setup file: jsdom ships neither encoder, and MSW's
// request/response plumbing needs both at module-init time.
import { TextDecoder, TextEncoder } from "node:util";

const target = globalThis as Record<string, unknown>;
target["TextEncoder"] ??= TextEncoder;
target["TextDecoder"] ??= TextDecoder;

// Nor does it lay anything out, so it has no `scrollIntoView`; the Run page calls it to land
// on the list a result tile linked to. A no-op a test can spy on is all jsdom can honestly give.
if (typeof Element !== "undefined") Element.prototype.scrollIntoView ??= function scrollIntoView() {};
