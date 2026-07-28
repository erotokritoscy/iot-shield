// Loaded before every other setup file: jsdom ships neither encoder, and MSW's
// request/response plumbing needs both at module-init time.
import { TextDecoder, TextEncoder } from "node:util";

const target = globalThis as Record<string, unknown>;
target["TextEncoder"] ??= TextEncoder;
target["TextDecoder"] ??= TextDecoder;
