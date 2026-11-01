import type { Vulnerability } from "@/types/issues";
import {
  parseRunJson,
  parseVulnerabilitiesJson,
  type ParsedRunSummary,
} from "@/lib/local-run-parser";

/**
 * Data seam for the local viewer: fetches against the local Python server's
 * JSON endpoints (same origin, relative URLs), producing the in-memory
 * `LoadedRun` shape the UI renders plus a `finished` flag driving live polling.
 *
 * The server serves a live in-progress run and a finished one identically; the
 * only signal is `run.finished`.
 */

/** A transcript agent as emitted by GET /api/transcript (already parsed). */
export interface TranscriptAgent {
  id: string;
  name: string;
  parent_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

/** Chat/tool event data as emitted by GET /api/transcript. */
export interface TranscriptEvent {
  id: string;
  type: "chat" | "tool";
  agent_id: string;
  timestamp: string;
  version: number;
  data: Record<string, unknown>;
}

export interface Transcript {
  agents: TranscriptAgent[];
  events: TranscriptEvent[];
}

/**
 * One MCP connection's non-secret status, as persisted to run.json by the
 * engine under `mcp_connection_status` and surfaced verbatim by GET /api/run.
 * Only name / provider / tool_count / dead ride here; never config, url, or
 * token. `dead` means the connection's live session gave up reconnecting.
 */
export interface McpConnectionStatus {
  name: string;
  provider: string | null;
  toolCount: number;
  dead: boolean;
}

/**
 * Read the MCP connection roster out of a raw run record. Tolerates the field
 * being absent (older runs, or a run with no MCP) and any malformed entry,
 * yielding an empty list rather than throwing.
 */
export function parseMcpConnectionStatus(raw: Record<string, unknown>): McpConnectionStatus[] {
  const list = raw?.mcp_connection_status;
  if (!Array.isArray(list)) return [];
  return list.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const record = entry as Record<string, unknown>;
    const name = typeof record.name === "string" ? record.name.trim() : "";
    if (!name) return [];
    const provider = typeof record.provider === "string" && record.provider.trim() ? record.provider.trim() : null;
    const toolCount = typeof record.tool_count === "number" ? record.tool_count : 0;
    const dead = record.dead === true;
    return [{ name, provider, toolCount, dead }];
  });
}

export interface LoadedRun {
  summary: ParsedRunSummary;
  /** Whole raw run record (for llm_usage, targets_info details, etc.). */
  raw: Record<string, unknown>;
  finished: boolean;
  vulnerabilities: Vulnerability[];
  reportMarkdown: string | null;
  transcript: Transcript;
}

async function getJson(path: string): Promise<unknown> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} responded ${res.status}`);
  return res.json();
}

/** Build a ``?run=<name>`` suffix for run-scoped data endpoints. */
function runQuery(runName?: string | null): string {
  return runName ? `?run=${encodeURIComponent(runName)}` : "";
}

export async function fetchRunSummary(runName?: string | null): Promise<{
  summary: ParsedRunSummary;
  raw: Record<string, unknown>;
  finished: boolean;
}> {
  const raw = (await getJson("/api/run" + runQuery(runName))) as Record<string, unknown>;
  // parseRunJson tolerates extra keys and takes raw TEXT.
  const summary = parseRunJson(JSON.stringify(raw));
  const finished = raw.finished === true;
  return { summary, raw, finished };
}

export async function fetchVulnerabilities(
  runId: string | null,
  runName?: string | null
): Promise<Vulnerability[]> {
  const arr = await getJson("/api/vulnerabilities" + runQuery(runName));
  return parseVulnerabilitiesJson(JSON.stringify(arr), runId);
}

export async function fetchReportMarkdown(runName?: string | null): Promise<string | null> {
  const obj = (await getJson("/api/report" + runQuery(runName))) as { markdown?: string };
  return obj?.markdown ?? null;
}

export async function fetchTranscript(runName?: string | null): Promise<Transcript> {
  const obj = (await getJson("/api/transcript" + runQuery(runName))) as Partial<Transcript>;
  return {
    agents: Array.isArray(obj?.agents) ? obj.agents : [],
    events: Array.isArray(obj?.events) ? obj.events : [],
  };
}

/** One-shot fetch of every endpoint (used on mount and on final settle). */
export async function fetchAll(runName?: string | null): Promise<LoadedRun> {
  const { summary, raw, finished } = await fetchRunSummary(runName);
  const [vulnerabilities, reportMarkdown, transcript] = await Promise.all([
    fetchVulnerabilities(summary.runId, runName).catch(() => [] as Vulnerability[]),
    fetchReportMarkdown(runName).catch(() => null),
    fetchTranscript(runName).catch(() => ({ agents: [], events: [] }) as Transcript),
  ]);
  return { summary, raw, finished, vulnerabilities, reportMarkdown, transcript };
}

// ---------------------------------------------------------------------------
// Local run history
//
// Run history uses the same local session capability as individual runs.
// ---------------------------------------------------------------------------

export interface RunSeverityCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
}

export interface RunListEntry {
  name: string;
  target: string | null;
  scan_mode: string | null;
  status: string | null;
  start_time: string | null;
  end_time: string | null;
  finished: boolean;
  severity_counts: RunSeverityCounts;
}

export interface RunsPayload {
  locked: boolean;
  count: number;
  runs: RunListEntry[];
}

export async function fetchRuns(): Promise<RunsPayload> {
  const obj = (await getJson("/api/runs")) as Partial<RunsPayload>;
  return {
    locked: obj?.locked ?? true,
    count: typeof obj?.count === "number" ? obj.count : 0,
    runs: Array.isArray(obj?.runs) ? (obj.runs as RunListEntry[]) : [],
  };
}

export interface Capabilities {
  can_steer: boolean;
  can_manage_scans: boolean;
}

export type SteerResult = { ok: true } | { ok: false; error: string };

async function postJson(path: string, body: Record<string, unknown>) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: Record<string, unknown> = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
  } catch {
    /* empty or non-JSON body */
  }
  return { ok: res.ok, status: res.status, data };
}

/** GET /api/capabilities. can_steer is true only inside a live in-TUI scan. */
export async function fetchCapabilities(): Promise<Capabilities> {
  const obj = (await getJson("/api/capabilities")) as Partial<Capabilities>;
  return { can_steer: obj?.can_steer === true, can_manage_scans: obj?.can_manage_scans === true };
}

export interface ScanJob {
  id: string;
  target: string;
  scan_mode: string;
  max_budget_usd: number;
  status: string;
  start_time: string;
  end_time: string | null;
  exit_code: number | null;
  error: string | null;
  has_run: boolean;
  llm_model?: string | null;
}

export async function fetchJobs(): Promise<ScanJob[]> {
  const data = await getJson("/api/jobs") as { jobs: ScanJob[] };
  return data.jobs;
}

export async function startJob(body: Record<string, unknown>): Promise<void> {
  const result = await postJson("/api/jobs", body);
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not start scan."));
}

export async function rerunJob(id: string): Promise<void> {
  const result = await postJson(`/api/jobs/${encodeURIComponent(id)}/rerun`, { authorized: true });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not rerun scan."));
}

export async function stopJob(id: string): Promise<void> {
  const result = await postJson(`/api/jobs/${encodeURIComponent(id)}/stop`, {});
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not stop scan."));
}

export async function fetchJobLog(id: string): Promise<string> {
  const data = await getJson(`/api/jobs/${encodeURIComponent(id)}/log`) as { text: string };
  return data.text;
}

export interface ModelPrices {
  input?: number | null;
  output?: number | null;
  cached_input?: number | null;
}

export interface LlmProvider {
  id: string;
  name: string;
  kind: "openai" | "openrouter" | "anthropic" | "ollama" | "custom";
  base_url: string;
  model: string;
  has_api_key: boolean;
  prices: Record<string, ModelPrices>;
}

export interface LlmSettings {
  providers: LlmProvider[];
  selected_id: string | null;
}

export interface ProviderModel { id: string; prices?: ModelPrices }

export async function fetchLlmSettings(): Promise<LlmSettings> {
  return await getJson("/api/llm/settings") as LlmSettings;
}

export async function updateLlmSettings(action: "providers" | "select" | "delete", body: Record<string, unknown>): Promise<LlmSettings> {
  const result = await postJson(`/api/llm/${action}`, body);
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not save LLM settings."));
  return result.data as unknown as LlmSettings;
}

export async function fetchProviderModels(id: string): Promise<ProviderModel[]> {
  const result = await postJson("/api/llm/models", { id });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not fetch models."));
  return result.data.models as ProviderModel[];
}

/** POST /api/agents/steer. Sends a steering instruction to a running agent. */
export async function steerAgent(agentId: string, message: string): Promise<SteerResult> {
  const { ok, data } = await postJson("/api/agents/steer", {
    agent_id: agentId,
    message,
  });
  if (ok && data.ok === true) return { ok: true };
  return { ok: false, error: String(data.error ?? "unavailable") };
}
