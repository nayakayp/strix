import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, AlertCircle, ChevronRight, FileText, Bot } from "lucide-react";
import type { Vulnerability, VulnerabilitySeverity } from "@/types/issues";
import { SEVERITY_COLORS } from "@/types/issues";
import { getSeverityDot } from "@/lib/vulnerability-utils";
import FindingPanel from "@/components/vulnerability/FindingPanel";
import { ContentSection } from "@/components/vulnerability/ContentSection";
import AgentBrowser from "@/components/live/AgentBrowser";
import { severityCounts, type ParsedRunSummary } from "@/lib/local-run-parser";
import {
  fetchOverview,
  fetchCapabilities,
  fetchRunSummary,
  fetchRuns,
  fetchTranscript,
  fetchVulnerabilities,
  deleteRun,
  parseMcpConnectionStatus,
  type LoadedRun,
  type RunsPayload,
  type Transcript,
} from "@/data/serverSource";
import { runTitle } from "@/lib/target-utils";
import Sidebar from "@/components/Sidebar";
import PastRunsView from "@/components/PastRunsView";
import ScanJobsView from "@/components/ScanJobsView";
import SettingsView from "@/components/SettingsView";
import { RunDetails } from "@/components/RunDetails";
import { TrustToast } from "@/components/TrustToast";
import { HostedContext } from "@/components/HostedGate";

export type View = "pentest" | "overview" | "agents" | "settings";

function readPage(): { view: View; run: string | null; creating: boolean } {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const page = params.get("page");
  const run = params.get("run");
  const tab = params.get("tab");
  return {
    view: page === "runners" || page === "integrations" || page === "settings" ? "settings" : run ? (tab === "agents" ? tab : "overview") : "pentest",
    run,
    creating: page === "pentest" && !run && params.get("action") === "new",
  };
}

const TRUST_BANNER =
  "Your findings stay on your machine. They're rendered here locally in your browser and never uploaded or stored by Natsuhat.";

const SEVERITY_ORDER: VulnerabilitySeverity[] = ["critical", "high", "medium", "low"];
const POLL_MS = 500;

export default function App() {
  const hosted = useContext(HostedContext);
  const [activeRun, setActiveRun] = useState<string | null>(() => readPage().run);
  const [run, setRun] = useState<LoadedRun | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [transcriptLoading, setTranscriptLoading] = useState(true);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [transcriptRetry, setTranscriptRetry] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const closeFinding = useCallback(() => setSelectedId(null), []);
  const [view, setView] = useState<View>(() => readPage().view);
  const [creating, setCreating] = useState(() => readPage().creating);
  const [runs, setRuns] = useState<RunsPayload | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Whether this viewer can steer a live scan (true only inside the in-TUI
  // launcher that shares the running scan's coordinator + event loop).
  const [canSteer, setCanSteer] = useState(false);
  const [canManageScans, setCanManageScans] = useState(false);
  const canManageSettings = hosted ? ["owner", "admin"].includes(hosted.role ?? "") && hosted.aal === "aal2" : canManageScans;

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await fetchRuns());
    } catch {
      /* history list is best-effort */
    }
  }, []);

  useEffect(() => {
    // Capabilities never change over a session, so fetch once on mount.
    fetchCapabilities()
      .then((caps) => {
        setCanSteer(caps.can_steer);
        setCanManageScans(caps.can_manage_scans);
        // Management uses /api/jobs; only read-only viewers need the history catalog.
        if (!caps.can_manage_scans) void refreshRuns();
      })
      .catch(() => {
        /* absence of steering is the safe default */
        void refreshRuns();
      });
  }, [refreshRuns]);

  // Live polling, scoped to the active run. Re-runs when the active run changes
  // so switching to a past run (?run=<name>) reloads its overview; a finished
  // run does a single overview fetch and stops. Activity loads separately.
  const finishedRef = useRef(false);
  useEffect(() => {
    if (!activeRun) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    finishedRef.current = false;

    const schedule = () => {
      timer = setTimeout(tick, POLL_MS);
    };

    const tick = async () => {
      if (cancelled) return;
      try {
        const { summary, raw, finished } = await fetchRunSummary(activeRun);
        if (cancelled) return;
        if (finished && !finishedRef.current) {
          finishedRef.current = true;
          const full = await fetchOverview(activeRun);
          if (!cancelled) setRun(full);
          return; // stop polling
        }
        const vulnerabilities = await fetchVulnerabilities(summary.runId, activeRun).catch(() => [] as Vulnerability[]);
        if (cancelled) return;
        setRun((prev) => ({
          summary,
          raw,
          finished,
          vulnerabilities,
          reportMarkdown: prev?.reportMarkdown ?? null,
        }));
        schedule();
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load run data.");
        schedule();
      }
    };

    (async () => {
      try {
        const full = await fetchOverview(activeRun);
        if (cancelled) return;
        setRun(full);
        if (full.finished) {
          finishedRef.current = true;
        } else {
          schedule();
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load run data.");
        schedule();
      }
    })();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeRun]);

  // Overview needs only the roster and MCP indicators. Transfer and poll event
  // bodies only while Agents is open; abort requests when navigating away.
  useEffect(() => {
    if (!activeRun || !run) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    // Completion refreshes keep the current inspector and agent selection mounted.
    setTranscriptError(null);

    const load = async () => {
      try {
        const data = await fetchTranscript(activeRun, {
          includeEvents: view === "agents",
          signal: controller.signal,
        });
        if (cancelled) return;
        setTranscript(data);
        setTranscriptError(null);
      } catch (e) {
        if (cancelled) return;
        setTranscriptError(e instanceof Error ? e.message : "Could not load agent activity.");
      } finally {
        if (!cancelled) {
          setTranscriptLoading(false);
          if (!run.finished) timer = setTimeout(load, POLL_MS);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [activeRun, view, run?.finished, transcriptRetry]);

  const counts = useMemo(
    () => (run ? severityCounts(run.vulnerabilities) : null),
    [run]
  );
  const selected = run?.vulnerabilities.find((v) => v.id === selectedId) ?? null;
  const agentCount = transcript?.agents.length ?? null;

  // The run's persisted MCP roster (from run.json via /api/run), plus the set of
  // connections with a tool call currently in flight. "In use" is derived here
  // from the connection-tagged tool events rather than carried on the roster:
  // an MCP dispatch event carries its connection name and a status that moves
  // running -> completed, so a connection is in use while one of its events is
  // still running. This mirrors the terminal UI's MCP panel exactly.
  const mcpConnections = useMemo(
    () => (run ? parseMcpConnectionStatus(run.raw) : []),
    [run]
  );
  const mcpInUse = useMemo(() => {
    const inUse = new Set<string>(transcript?.mcp_in_use ?? []);
    for (const event of transcript?.events ?? []) {
      if (event.type !== "tool") continue;
      const connection = event.data?.mcp_connection;
      if (typeof connection !== "string" || !connection) continue;
      if (event.data?.status === "running") inUse.add(connection);
    }
    return inUse;
  }, [transcript]);

  useEffect(() => {
    const onNavigate = () => {
      const page = readPage();
      setView(page.view);
      setCreating(page.creating);
      if (page.view === "agents" && view !== page.view) setTranscriptLoading(true);
      if (activeRun !== page.run) {
        setRun(null);
        setTranscript(null);
        setTranscriptLoading(true);
        setTranscriptError(null);
        setError(null);
      }
      setActiveRun(page.run);
      setSelectedId(null);
    };
    window.addEventListener("hashchange", onNavigate);
    return () => window.removeEventListener("hashchange", onNavigate);
  }, [activeRun, view]);

  const userSetView = useCallback((v: View, name?: string) => {
    setSelectedId(null);
    const params = new URLSearchParams({ page: v === "settings" ? "settings" : "pentest" });
    if (v !== "pentest" && v !== "settings") {
      params.set("run", name ?? activeRun ?? "");
      params.set("tab", v);
    }
    window.location.hash = params.toString();
  }, [activeRun]);

  const selectRun = useCallback((name: string, finished?: boolean) => {
    const entry = runs?.runs.find(item => item.name === name);
    userSetView((finished ?? entry?.finished) === false ? "agents" : "overview", name);
  }, [runs, userSetView]);

  async function removeRun(name: string) {
    if (deleting || !window.confirm(hosted ? "Delete this scan, its findings, and its private stored artifacts? This cannot be undone. Retained E2B sandboxes will not be deleted." : "Delete this scan and all its local findings, reports, and logs? This cannot be undone. Retained remote sandboxes will not be deleted.")) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteRun(name);
      await refreshRuns();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "Could not delete scan.");
    } finally {
      setDeleting(false);
    }
  }

  const isRunView = view === "overview" || view === "agents";

  return (
    <div className="min-h-screen bg-black text-white flex">
      <Sidebar
        view={view}
        onSelectView={(v) => {
          setSelectedId(null);
          userSetView(v);
        }}
        canManageScans={canManageSettings}
        hosted={!!hosted}
        mcpConnections={isRunView ? mcpConnections : []}
        mcpInUse={mcpInUse}
      />

      <div ref={contentRef} className="flex-1 min-w-0">
        {/* Top bar */}
        <div className="border-b border-[#222]">
          <div className="max-w-[88rem] mx-auto px-3 sm:px-6 py-4 flex flex-wrap items-center gap-1.5 gap-y-3">
            <div className="flex shrink-0 items-center gap-1.5 lg:hidden">
              <img src="./logo.png" alt="Natsuhat" className="w-10 h-8 object-cover" />
              <div className="text-base text-white font-medium tracking-tight">Natsuhat</div>
            </div>
            <span className="hidden text-sm text-[#888] lg:inline">{view === "settings" ? "Settings" : "Pentest"}{isRunView ? " / Scan details" : creating ? " / New scan" : ""}</span>
            <nav aria-label="Main navigation" className="ml-auto flex flex-wrap items-center gap-4 lg:hidden">
              <TabButton active={view === "pentest" || isRunView} onClick={() => userSetView("pentest")}>Pentest</TabButton>
              {canManageSettings && <TabButton active={view === "settings"} onClick={() => userSetView("settings")}>Settings</TabButton>}
            </nav>
          </div>
        </div>

        <div className="max-w-[88rem] mx-auto px-3 sm:px-6 py-8 sm:py-12 space-y-6">
          {error && !run && isRunView && (
            <div className="rounded-lg px-4 py-3 flex gap-3 items-start border border-red-500/30 bg-red-500/5">
              <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5 text-red-400" aria-hidden="true" />
              <p className="text-sm text-red-300">{error}</p>
            </div>
          )}

          {/* Finding selection must not remount the overview or reset its scroll. */}
          <div
            key={`${activeRun ?? "launched"}:${view}`}
            className="animate-page-in space-y-6"
          >
          {view === "settings" ? (
            canManageSettings ? <SettingsView /> : <p className="text-sm text-[#888]">Settings require administrator access.</p>
          ) : view === "pentest" ? (
            <div className="space-y-4">
              {deleteError && <p role="alert" className="text-sm text-red-400">{deleteError}</p>}
              {canManageScans ? (
                <ScanJobsView creating={creating} onCreatingChange={value => { window.location.hash = value ? "page=pentest&action=new" : "page=pentest"; }} onSelectRun={selectRun} onDeleteRun={removeRun} deleting={deleting} canDelete={!hosted || canManageSettings} />
              ) : (
                <>
                  <h1 className="text-2xl font-semibold">Pentest</h1>
                  <p className="text-sm text-[#888]">{hosted ? hosted.role === "viewer" ? "Read-only access to workspace scans and findings." : "Browse workspace scans and findings. Verify MFA in Account & team to enable scan execution." : "Browse local scans and their findings."}</p>
                  <PastRunsView runs={runs} activeRun={activeRun} onSelectRun={selectRun} />
                </>
              )}
            </div>
          ) : !run && !error ? (
            <div className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-10 text-center">
              <div className="w-6 h-6 mx-auto mb-3 rounded-full border-2 border-[#333] border-t-white animate-spin" />
              <p className="text-sm text-[#888]">Loading run data…</p>
            </div>
          ) : run && counts ? (
            <>
              <button onClick={() => userSetView("pentest")} className="cursor-pointer inline-flex items-center gap-1.5 text-sm text-[#888] hover:text-white transition-colors">
                <ArrowLeft className="h-4 w-4" aria-hidden="true" />All scans
              </button>
              <SummaryHeader summary={run.summary} raw={run.raw} finished={run.finished} />

              <div className="flex gap-5 border-b border-[#2a2a2a]" role="navigation" aria-label="Scan details">
                <TabButton active={view === "overview"} onClick={() => userSetView("overview")}>
                  Overview
                </TabButton>
                {(!run.finished || agentCount !== 0) && (
                  <TabButton active={view === "agents"} onClick={() => userSetView("agents")}>
                    Agents{agentCount === null ? "" : ` (${agentCount})`}
                  </TabButton>
                )}
              </div>

              {view === "overview" ? (
                <OverviewTab
                  summary={run.summary}
                  counts={counts}
                  vulnerabilities={run.vulnerabilities}
                  finished={run.finished}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                  agentCount={agentCount}
                  reportMarkdown={run.reportMarkdown}
                  raw={run.raw}
                />
              ) : view === "agents" ? (transcriptLoading ? (
                <p role="status" className="rounded-xl border border-[#222] p-8 text-center text-sm text-[#888]">Loading agent activity…</p>
              ) : transcriptError ? (
                <div role="alert" className="rounded-xl border border-red-500/30 p-5 text-sm text-red-300">
                  <p>Could not load agent activity.</p>
                  <button type="button" onClick={() => {
                    setTranscriptLoading(true);
                    setTranscriptRetry(value => value + 1);
                  }} className="mt-3 cursor-pointer underline">Retry</button>
                </div>
              ) : transcript ? (
                <AgentBrowser transcript={transcript} finished={run.finished} canSteer={canSteer} />
              ) : null) : null}
            </>
          ) : null}
          </div>
        </div>
        {view === "overview" && selected && run && <FindingPanel
          vulnerability={selected}
          vulnerabilities={run.vulnerabilities}
          containerRef={contentRef}
          onSelect={setSelectedId}
          onClose={closeFinding}
        />}
      </div>
      <TrustToast message={hosted ? "Workspace records and private scan artifacts are stored in your configured Supabase project." : TRUST_BANNER} />
    </div>
  );
}

function formatDuration(seconds: number | null): string | null {
  if (seconds == null) return null;
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function SummaryHeader({ summary, raw, finished }: { summary: ParsedRunSummary; raw: Record<string, unknown>; finished: boolean }) {
  const duration = formatDuration(summary.durationSeconds);
  const cost = (raw.llm_usage as { cost?: number } | undefined)?.cost;
  const status = summary.status ?? (finished ? "Finished" : "Running");
  const failed = status === "failed" || status === "crashed";
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold text-white">
          {runTitle(summary.targets[0] ?? null, summary.runName ?? summary.runId ?? "Pentest results")}
        </h1>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-[#888]">
          {summary.targets.length > 0 && (
            <span className="break-all font-mono text-[#aaa]">{summary.targets.join(", ")}</span>
          )}
          {summary.scanMode && <Meta label={summary.scanMode} />}
          {duration && <Meta label={duration} />}
          {typeof cost === "number" && Number.isFinite(cost) && <Meta label={`$${cost.toFixed(2)} LLM`} />}
        </div>
      </div>
      <span className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium capitalize ${failed ? "border-red-500/30 bg-red-500/5 text-red-300" : finished ? "border-[#333] text-[#aaa]" : "border-emerald-500/30 bg-emerald-500/5 text-emerald-300"}`}>
        <span className={`h-1.5 w-1.5 rounded-full ${failed ? "bg-red-400" : finished ? "bg-[#888]" : "animate-pulse bg-emerald-400"}`} aria-hidden="true" />{status.replaceAll("_", " ")}
      </span>
    </div>
  );
}

function Meta({ label }: { label: string }) {
  return (
    <>
      <span className="text-[#333]">·</span>
      <span className="capitalize">{label}</span>
    </>
  );
}

function FindingsList({
  vulnerabilities,
  finished,
  selectedId,
  onSelect,
}: {
  vulnerabilities: Vulnerability[];
  finished: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const sorted = [...vulnerabilities].sort(
    (a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)
  );
  if (sorted.length === 0) {
    return (
      <div className="space-y-4">
        <div className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-8 text-center text-sm text-[#888]">
          {finished ? "No findings in this run." : "No findings yet. The pentest is still running…"}
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {sorted.map((v) => (
        <button
          key={v.id}
          aria-expanded={selectedId === v.id}
          aria-controls={selectedId === v.id ? "finding-panel" : undefined}
          onClick={() => onSelect(v.id)}
          className={`cursor-pointer w-full text-left rounded-xl border px-4 py-4 transition-colors flex items-center gap-3 focus-visible:outline-emerald-400 ${selectedId === v.id ? "border-emerald-500/40 bg-emerald-500/5" : "border-[#222] hover:border-[#444] bg-white/[0.02]"}`}
        >
          <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${getSeverityDot(v.severity)}`} aria-hidden="true" />
          <span className="flex-1 min-w-0">
            <span className="block text-sm font-medium text-white truncate">{v.title}</span>
            {v.target && (
              <span className="block text-xs text-[#666] font-mono truncate">{v.target}</span>
            )}
          </span>
          <span
            className={`text-xs font-semibold px-2 py-0.5 rounded-full border capitalize ${SEVERITY_COLORS[v.severity]}`}
          >
            {v.severity}
          </span>
          <ChevronRight className="h-4 w-4 shrink-0 text-[#666]" aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}

/** Strip a single leading markdown heading (report sections embed their own). */
function stripLeadingHeading(md: string): string {
  return md.replace(/^\s*#{1,6}[ \t]+.*(?:\r?\n)+/, "").trimStart();
}

function dedupeHeadings(md: string): string {
  const out: string[] = [];
  let lastHeading: string | null = null;
  for (const line of md.split("\n")) {
    const m = line.match(/^#{1,6}\s+(.*)$/);
    if (m) {
      const norm = m[1].trim().toLowerCase();
      if (norm === lastHeading) continue;
      lastHeading = norm;
    } else if (line.trim() !== "") {
      lastHeading = null;
    }
    out.push(line);
  }
  return out.join("\n");
}

function OverviewTab({
  summary,
  counts,
  vulnerabilities,
  finished,
  selectedId,
  onSelect,
  agentCount,
  reportMarkdown,
  raw,
}: {
  summary: ParsedRunSummary;
  counts: Record<VulnerabilitySeverity, number>;
  vulnerabilities: Vulnerability[];
  finished: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  agentCount: number | null;
  reportMarkdown: string | null;
  raw: Record<string, unknown>;
}) {
  const summaryMarkdown = summary.executiveSummary || reportMarkdown;
  const sections = (
    [
      ["Executive Summary", summary.executiveSummary],
      ["Technical Analysis", summary.technicalAnalysis],
      ["Methodology", summary.methodology],
      ["Recommendations", summary.recommendations],
    ] as const
  )
    .filter(([, content]) => !!content)
    .map(([title, content]) => ({ title, content: stripLeadingHeading(content as string) }));

  return (
    <div className="space-y-6">
      <section aria-label="Summary" className="flex gap-4 rounded-xl border border-[#222] bg-white/[0.02] p-5">
        <FileText className="mt-0.5 h-5 w-5 shrink-0 text-[#888]" aria-hidden="true" />
        <div className="min-w-0 flex-1 space-y-3">
          <h2 className="text-sm font-semibold">Summary</h2>
          {summaryMarkdown ? <div className="line-clamp-4"><ContentSection content={stripLeadingHeading(summaryMarkdown)} /></div> : <p className="text-sm text-[#aaa]">{finished ? `Scan ${summary.status?.replaceAll("_", " ") ?? "finished"}. No summary was saved.` : "Scan in progress. The summary will appear here when available."}</p>}
          <p className="flex items-center gap-2 text-xs text-[#888]"><Bot className="h-3.5 w-3.5" aria-hidden="true" />{agentCount === null ? "Loading agent count…" : `${agentCount} ${agentCount === 1 ? "agent" : "agents"}`}</p>
        </div>
      </section>

      <section aria-label="Findings" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Findings <span className="text-[#888]">({vulnerabilities.length})</span></h2>
          <div className="flex flex-wrap gap-3 text-xs text-[#aaa]" aria-label="Finding severity counts">
            {SEVERITY_ORDER.filter(severity => counts[severity] > 0).map(severity => <span key={severity} className="inline-flex items-center gap-1.5"><span className={`h-2 w-2 rounded-full ${getSeverityDot(severity)}`} aria-hidden="true" />{counts[severity]} {severity}</span>)}
          </div>
        </div>
        <FindingsList vulnerabilities={vulnerabilities} finished={finished} selectedId={selectedId} onSelect={onSelect} />
      </section>

      {(sections.length > 0 || reportMarkdown) && <details className="rounded-xl border border-[#222] bg-white/[0.02] p-5">
        <summary className="cursor-pointer text-sm font-semibold text-[#ccc]">Full report</summary>
        <div className="mt-5 space-y-8">
          {sections.length > 0 ? sections.map(section => <ContentSection key={section.title} title={section.title} content={section.content} />) : <ContentSection content={dedupeHeadings(reportMarkdown!)} />}
        </div>
      </details>}

      <RunDetails raw={raw} durationSeconds={summary.durationSeconds} />
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`cursor-pointer relative pb-2.5 text-sm font-semibold transition-colors ${
        active ? "text-white" : "text-[#666] hover:text-white"
      }`}
    >
      {children}
      {active && <span className="absolute bottom-0 inset-x-0 h-0.5 bg-emerald-400 rounded-full" />}
    </button>
  );
}
