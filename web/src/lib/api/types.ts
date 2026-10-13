/**
 * The API surface, in TypeScript, generated from both apps' own OpenAPI documents (D4).
 *
 * `read.gen.ts` / `pipeline.gen.ts` are machine-written and committed — never edit them;
 * run `npm run gen:types` after either FastAPI app changes. This module is the hand-written
 * index over them, so the rest of the app imports readable names instead of
 * `components["schemas"]["…"]` chains.
 */
import type { components as ReadComponents } from "./read.gen";
import type { components as PipelineComponents } from "./pipeline.gen";

type R = ReadComponents["schemas"];
type P = PipelineComponents["schemas"];

// --- Read plane (the contracts, which ARE the wire format — ADR-0003) ---------
export type Asset = R["Asset"];
export type NormalizedEvent = R["NormalizedEvent"];
export type DetectionResult = R["DetectionResult"];
export type PrioritizedAlert = R["PrioritizedAlert"];
export type MalformedRecord = R["MalformedRecord"];
export type Summary = R["Summary"];
export type Lane = R["Lane"];
export type AlertSeverity = R["AlertSeverity"];
export type Criticality = R["Criticality"];

// --- Pipeline plane (orchestration) ------------------------------------------
export type ParamInfo = P["ParamInfo"];
export type ModuleInfo = P["ModuleInfo"];
export type SinkInfo = P["SinkInfo"];
export type ModulesResponse = P["ModulesResponse"];
export type IssueInfo = P["IssueInfo"];
export type ValidateResponse = P["ValidateResponse"];
export type RunResponse = P["RunResponse"];
export type PipelineSpec = P["PipelineSpec"];

// --- Accounts (mounted only when IOTSHIELD_AUTH is set — ADR-0039) -----------
export type MeResponse = P["MeResponse"];
export type UserInfo = P["UserInfo"];
export type Role = UserInfo["role"];
/** A role inside an organization — the super admin is not one (ADR-0040). */
export type OrgRole = P["NewUserRequest"]["role"] & string;
export type AuthOptions = P["AuthOptions"];
export type OrganizationAdminInfo = P["OrganizationAdminInfo"];

// --- Evaluation plane (mounted only when IOTSHIELD_EVAL_API is set) ----------
export type CorpusSummary = P["CorpusSummary"];
export type CorporaResponse = P["CorporaResponse"];
export type DetectorSummary = P["DetectorSummary"];
export type BenchSpecSummary = P["BenchSpecSummary"];
export type EvaluationReport = P["EvaluationReport"];
export type SpecReport = P["SpecReport"];
export type CaseReport = P["CaseReport"];
export type CaseTimeline = P["CaseTimeline"];
export type FitProtocol = P["FitProtocol"];

/** One `/eval/run` entry: a spec, the label it appears under, and an optional sweep. */
export type LabelledSpec = P["LabelledSpec"];

/**
 * The severity axis, in the order the chart and the badges use it.
 *
 * Fixed rather than derived from the data: a severity absent from this run must still hold
 * its place, or the axis silently re-scales between refreshes and two screenshots of the
 * same queue stop being comparable (T3).
 */
export const SEVERITY_ORDER: readonly AlertSeverity[] = [
  "low",
  "medium",
  "high",
  "critical",
  "emergency",
] as const;

/** The KPI keys of `/summary.counts`, in the order the tiles show them (T1). */
export const SUMMARY_KEYS = ["events", "detections", "alerts", "malformed_records"] as const;
