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
  mcp_in_use?: string[];
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
}

// Tab-local context, updated only when HostedGate displays a new session.
// Another tab's session-cookie workspace selection must not retarget requests.
let hostedWorkspace: string | undefined;

export function setHostedWorkspace(workspace: string | undefined) {
  hostedWorkspace = workspace;
}

export function workspaceHeaders(): Record<string, string> {
  return hostedWorkspace ? { "X-Natsuhat-Workspace": hostedWorkspace } : {};
}

async function getJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const res = await fetch(path, { cache: "no-store", headers: workspaceHeaders(), signal });
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

export async function fetchTranscript(
  runName?: string | null,
  { includeEvents = true, signal }: { includeEvents?: boolean; signal?: AbortSignal } = {}
): Promise<Transcript> {
  let query = runQuery(runName);
  if (!includeEvents) query += `${query ? "&" : "?"}events=0`;
  const obj = (await getJson("/api/transcript" + query, signal)) as Partial<Transcript>;
  return {
    agents: Array.isArray(obj?.agents) ? obj.agents : [],
    events: Array.isArray(obj?.events) ? obj.events : [],
    mcp_in_use: Array.isArray(obj?.mcp_in_use) ? obj.mcp_in_use.filter(name => typeof name === "string" && name) : undefined,
  };
}

/** Overview data never waits for the agent activity stream. */
export async function fetchOverview(runName?: string | null): Promise<LoadedRun> {
  const { summary, raw, finished } = await fetchRunSummary(runName);
  const [vulnerabilities, reportMarkdown] = await Promise.all([
    fetchVulnerabilities(summary.runId, runName).catch(() => [] as Vulnerability[]),
    fetchReportMarkdown(runName).catch(() => null),
  ]);
  return { summary, raw, finished, vulnerabilities, reportMarkdown };
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
    headers: { "Content-Type": "application/json", ...workspaceHeaders() },
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
  source?: SourceSnapshot;
  scan_mode: string;
  max_budget_usd: number;
  status: string;
  start_time: string;
  end_time: string | null;
  exit_code: number | null;
  error: string | null;
  has_run: boolean;
  can_resume?: boolean;
  resume_unavailable_reason?: string | null;
  llm_model?: string | null;
  runner?: "local" | "e2b" | "daytona";
  remote?: { phase: string; sandbox_id?: string; cleanup: string; last_sync?: string; sync_error?: string | null; resume_count?: number; results_pending?: boolean };
}

export interface JobsPayload {
  jobs: ScanJob[];
  runners: { id: string; name: string; configured: boolean }[];
  default_runner: "e2b";
  max_remote_concurrent: number;
  remote_timeout_seconds: number;
}

export interface RunnerCheck {
  template: string;
  template_id: string | null;
  build_id: string | null;
  revision: string | null;
  resolved_template: string | null;
  release: string | null;
  checked_at: string;
  availability: "available" | "missing" | "unavailable" | "unconfigured" | "error";
  compatibility: "release_pinned" | "incompatible" | "unverified";
  message: string;
}

export interface RunnerSettings {
  effective_default_runner: "e2b";
  e2b_template: string;
  images: {
    id: "e2b";
    name: string;
    configured: boolean;
    template: string;
    selection_source: "release";
    check: RunnerCheck | null;
  }[];
}

export async function fetchRunnerSettings(): Promise<RunnerSettings> {
  return await getJson("/api/runners/settings") as RunnerSettings;
}

export async function checkRunner(provider: string): Promise<RunnerCheck> {
  const result = await postJson("/api/runners/check", { provider });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not check runner image."));
  return result.data as unknown as RunnerCheck;
}

export async function fetchJobState(): Promise<JobsPayload> {
  return await getJson("/api/jobs") as JobsPayload;
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

export async function resumeJob(id: string, max_budget_usd: number): Promise<void> {
  const result = await postJson(`/api/jobs/${encodeURIComponent(id)}/resume`, { authorized: true, max_budget_usd });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not resume scan."));
}

export async function stopJob(id: string): Promise<void> {
  const result = await postJson(`/api/jobs/${encodeURIComponent(id)}/stop`, {});
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not stop scan."));
}

export async function deleteRun(name: string): Promise<void> {
  const result = await postJson(`/api/runs/${encodeURIComponent(name)}/delete`, { confirmed: true });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not delete scan."));
}

export async function fetchJobLog(id: string): Promise<string> {
  const data = await getJson(`/api/jobs/${encodeURIComponent(id)}/log`) as { text: string };
  return data.text;
}

export interface SourceSnapshot {
  kind: "zip" | "github";
  filename: string;
  sha256: string;
  files: number;
  size: number;
  warnings: string[];
  repository?: string;
  repository_id?: number;
  ref?: string;
  commit?: string;
  upload_id?: string;
}

export type ScanSource = { kind: "zip"; upload_id: string } | { kind: "github"; repository_id: number; ref: string };
export interface GithubRepository { id: number; full_name: string; default_branch: string; private: boolean }
export interface GithubState { configured: boolean; connected: boolean; repositories: GithubRepository[]; available: GithubRepository[] }

export async function fetchGithub(refresh = false): Promise<GithubState> {
  return await getJson(refresh ? "/api/github/refresh" : "/api/github") as GithubState;
}

export async function checkGithubSource(repository_id: number, ref: string): Promise<{ repository_id: number; ref: string; commit: string }> {
  const result = await postJson("/api/github/check", { repository_id, ref });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not check this repository. Check again before scanning."));
  return result.data as unknown as { repository_id: number; ref: string; commit: string };
}

export async function updateGithub(action: string, id?: number): Promise<GithubState & { url?: string }> {
  const result = await postJson(`/api/github/${action}`, { id });
  if (!result.ok) throw new Error(String(result.data.error ?? "Could not update GitHub connection."));
  return result.data as unknown as GithubState & { url?: string };
}

export function uploadSource(file: File, progress: (value: number) => void, signal: AbortSignal): Promise<SourceSnapshot> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/sources/upload");
    request.setRequestHeader("Content-Type", "application/zip");
    request.setRequestHeader("X-Source-Filename", encodeURIComponent(file.name));
    for (const [name, value] of Object.entries(workspaceHeaders())) request.setRequestHeader(name, value);
    request.responseType = "json";
    request.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
    const abort = () => request.abort();
    signal.addEventListener("abort", abort, { once: true });
    request.onloadend = () => signal.removeEventListener("abort", abort);
    request.onload = () => request.status === 201 ? resolve(request.response as SourceSnapshot) : reject(new Error(request.response?.error ?? "ZIP upload failed."));
    request.onerror = () => reject(new Error("ZIP upload failed. Check your connection."));
    request.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    if (signal.aborted) reject(new DOMException("Upload cancelled", "AbortError"));
    else request.send(file);
  });
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
  extra_header_names?: string[];
  extra_body?: Record<string, unknown>;
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
