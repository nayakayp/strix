import { useEffect, useId, useState, type ButtonHTMLAttributes, type FormEvent } from "react";
import { ArrowLeft, ChevronRight, FileArchive, GitBranch, Globe, LoaderCircle, Play, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import { checkGithubSource, checkRunner, fetchJobLog, fetchJobs, fetchJobState, fetchLlmSettings, rerunJob, resumeJob, startJob, stopJob, type ScanJob, type JobsPayload, type ScanSource, type RunnerCheck } from "@/data/serverSource";
import ScanSourceFields from "@/components/ScanSourceFields";
import { RunnerCheckStatus } from "@/components/RunnerSettingsView";

const ACTIVE = new Set(["starting", "running", "stopping"]);
const inputClass = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const buttonClass = "cursor-pointer rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

function ScanAction({ label, children, className = "", onClick, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return <span className="relative inline-flex" tabIndex={props.disabled ? 0 : undefined} aria-label={props.disabled ? label : undefined} aria-describedby={props.disabled && open ? id : undefined} onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={event => { if (event.key === "Escape") { setOpen(false); event.stopPropagation(); } }}>
    <button {...props} type="button" aria-label={label} aria-describedby={open ? id : undefined} onClick={event => { setOpen(false); onClick?.(event); }} className={`${buttonClass} !p-2.5 focus-visible:outline-emerald-400 ${className}`}>{children}</button>
    {open && <span id={id} role="tooltip" className="absolute right-0 top-full z-30 w-max max-w-56 pt-2"><span className="block rounded-md border border-[#444] bg-[#181818] px-2.5 py-1.5 text-xs text-[#eee] shadow-xl">{label}</span></span>}
  </span>;
}

function failureMessage(log: string, fallback: string): string {
  // Explicit runner failures (for example, lost results) take precedence over scan output.
  if (fallback !== "Scan exited with an error. Check the job log." && fallback !== "Scan failed.") return fallback;
  const text = log.split("\n").map(line => line.replace(/^│\s*|\s*│$/g, "").trim()).join("\n");
  const errors = [...text.matchAll(/^(?:Error:|natsuhat: error:)\s*(.+(?:\n[^\n]+)*)/gim)];
  const detail = errors.at(-1)?.[1].replace(/\s+/g, " ").trim()
    ?? [...text.matchAll(/^[\w.]*(?:Error|Exception): .+$/gm)].at(-1)?.[0];
  if (!detail) return fallback;
  // Providers may print either JSON or Python-style dictionaries in CLI errors.
  const message = detail.match(/['"]message['"]:\s*(['"])((?:\\.|(?!\1).)*)\1/)?.[2];
  const status = detail.match(/Error code:\s*(\d+)/i)?.[1];
  const reason = message ? `${status ? `HTTP ${status}: ` : ""}${message}` : detail;
  return text.includes("LLM CONNECTION FAILED") ? `LLM connection failed. ${reason}` : reason;
}

function ScanFailure({ job }: { job: ScanJob }) {
  const [log, setLog] = useState<string | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLog(null);
    setLogError(null);
    fetchJobLog(job.id).then(text => {
      if (!cancelled) setLog(text.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, ""));
    }).catch(error => {
      if (!cancelled) setLogError(error instanceof Error ? error.message : "Could not load the failure log.");
    });
    return () => { cancelled = true; };
  }, [job.id, attempt]);
  const fallback = job.error || job.remote?.sync_error || "Scan failed.";
  return <div className="relative z-10 mt-3 space-y-2">
    <p role="alert" className="break-words text-xs leading-relaxed text-red-400">{failureMessage(log ?? "", fallback)}</p>
    <details className="rounded-lg border border-[#292929] bg-black/30">
      <summary className="cursor-pointer rounded-lg px-3 py-2 text-xs text-[#aaa] hover:text-white focus-visible:outline-emerald-400">Failure log</summary>
      <div className="border-t border-[#292929] p-3">
        {logError ? <div className="space-y-2">
          <p role="alert" className="break-words text-xs text-red-400">{logError}</p>
          <button type="button" className={buttonClass} onClick={() => setAttempt(value => value + 1)}>Retry loading log</button>
        </div> : log === null ? <p role="status" className="text-xs text-[#888]">Loading failure log…</p>
          : <pre aria-label="Failure log output" tabIndex={0} className="max-h-64 overflow-auto whitespace-pre text-[11px] leading-relaxed text-[#aaa] scrollbar-thin focus-visible:outline-emerald-400">{log || "No log output was saved for this scan."}</pre>}
      </div>
    </details>
  </div>;
}

export default function ScanJobsView({ creating, onCreatingChange, onSelectRun, onDeleteRun, deleting }: {
  creating: boolean;
  onCreatingChange: (value: boolean) => void;
  onSelectRun: (name: string) => void;
  onDeleteRun: (name: string) => Promise<void>;
  deleting: boolean;
}) {
  const [jobs, setJobs] = useState<ScanJob[] | null>(null);
  const [target, setTarget] = useState("");
  const [source, setSource] = useState<ScanSource | null>(null);
  const [mode, setMode] = useState("quick");
  const [budget, setBudget] = useState("10");
  const [instruction, setInstruction] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [llm, setLlm] = useState<string | null>(null);
  const [rerunId, setRerunId] = useState<string | null>(null);
  const [rerunAuthorized, setRerunAuthorized] = useState(false);
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [resumeAuthorized, setResumeAuthorized] = useState(false);
  const [resumeBudget, setResumeBudget] = useState("");
  const [configuration, setConfiguration] = useState<JobsPayload | null>(null);
  const [runnerCheck, setRunnerCheck] = useState<RunnerCheck | null>(null);
  const [runnerCheckError, setRunnerCheckError] = useState<string | null>(null);
  const [runnerCheckAttempt, setRunnerCheckAttempt] = useState(0);
  const [githubCheck, setGithubCheck] = useState<{ repository_id: number; ref: string; commit?: string; error?: string } | null>(null);
  const [githubCheckAttempt, setGithubCheckAttempt] = useState(0);
  const repositoryId = source?.kind === "github" ? source.repository_id : 0;
  const repositoryRef = source?.kind === "github" ? source.ref.trim() : "";
  const currentGithubCheck = githubCheck?.repository_id === repositoryId && githubCheck.ref === repositoryRef ? githubCheck : null;
  const sourceBlocked = source?.kind === "github" && !currentGithubCheck?.commit;
  const checked = runnerCheck;
  const imageBlocked = !checked || checked.availability !== "available" || checked.compatibility === "incompatible";

  useEffect(() => {
    let cancelled = false;
    setGithubCheck(null);
    if (!creating || !repositoryId || !repositoryRef) return;
    const timer = setTimeout(() => {
      checkGithubSource(repositoryId, repositoryRef)
        .then(result => { if (!cancelled) setGithubCheck(result); })
        .catch(e => { if (!cancelled) setGithubCheck({ repository_id: repositoryId, ref: repositoryRef, error: e instanceof Error ? e.message : "Could not check this repository. Check again before scanning." }); });
    }, 350);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [creating, repositoryId, repositoryRef, githubCheckAttempt]);

  useEffect(() => {
    let cancelled = false;
    setRunnerCheck(null);
    setRunnerCheckError(null);
    if (creating) {
      document.getElementById("new-scan-title")?.focus();
      window.scrollTo(0, 0);
      checkRunner("e2b").then(result => { if (!cancelled) setRunnerCheck(result); }).catch(e => { if (!cancelled) setRunnerCheckError(e.message); });
    } else {
      document.getElementById("new-scan-trigger")?.focus();
      setSource(null);
      setAuthorized(false);
      setError(null);
    }
    return () => { cancelled = true; };
  }, [creating, runnerCheckAttempt]);

  function atCapacity() {
    const running = jobs?.filter(job => ACTIVE.has(job.status)) ?? [];
    return running.length >= (configuration?.max_remote_concurrent ?? 0);
  }
  const active = atCapacity();
  const unlimitedBudget = Number(budget) === -1;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [state, config] = await Promise.all([fetchJobState(), fetchLlmSettings()]);
        const provider = config.providers.find(p => p.id === config.selected_id);
        if (!cancelled) {
          setJobs(state.jobs);
          setConfiguration(state);
          setLlm(provider ? `${provider.name} · ${provider.model}` : "Server CLI settings");
          setPollError(null);
        }
      } catch (e) {
        if (!cancelled) setPollError(e instanceof Error ? e.message : "Could not load jobs.");
      } finally {
        if (!cancelled) timer = setTimeout(poll, 1000);
      }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending || !authorized || active || imageBlocked || sourceBlocked || (source?.kind === "zip" && !source.upload_id)) return;
    setPending(true);
    setError(null);
    try {
      await startJob({ target, ...(source ? { source } : {}), runner: "e2b", scan_mode: mode, max_budget_usd: Number(budget), instruction, authorized });
      setAuthorized(false);
      onCreatingChange(false);
      setSource(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start scan.");
    } finally { setPending(false); }
  }

  async function stop(id: string) {
    setPending(true);
    setError(null);
    try {
      await stopJob(id);
      setJobs(await fetchJobs());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop scan.");
    } finally { setPending(false); }
  }

  async function rerun(id: string) {
    if (pending || !rerunAuthorized) return;
    setPending(true);
    setError(null);
    try {
      await rerunJob(id);
      setRerunId(null);
      setRerunAuthorized(false);
      setJobs(await fetchJobs());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not rerun scan.");
    } finally { setPending(false); }
  }

  async function resume(id: string) {
    if (pending || !resumeAuthorized) return;
    setPending(true);
    setError(null);
    try {
      await resumeJob(id, Number(resumeBudget));
      setResumeId(null);
      setResumeAuthorized(false);
      setJobs(await fetchJobs());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resume scan.");
    } finally { setPending(false); }
  }

  if (creating) return <div className="mx-auto max-w-4xl space-y-7">
    <button type="button" disabled={pending} onClick={() => onCreatingChange(false)} className="inline-flex cursor-pointer items-center gap-2 text-sm text-[#888] hover:text-white disabled:opacity-40"><ArrowLeft className="h-4 w-4" aria-hidden="true" />Back to scans</button>
    <h1 id="new-scan-title" tabIndex={-1} className="text-2xl font-semibold outline-none sm:text-3xl">New scan</h1>
    <form id="create-scan" aria-label="New scan" onChange={() => setError(null)} onSubmit={submit}>
        <fieldset disabled={pending || jobs === null} className="space-y-7">
          <ScanSourceFields target={target} onTargetChange={value => { setTarget(value); setAuthorized(false); }} source={source} onChange={value => { setSource(value); setAuthorized(false); }} githubCheck={currentGithubCheck} onCheckAgain={() => { setGithubCheck(null); setGithubCheckAttempt(value => value + 1); }} />
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm text-[#aaa]">Scan mode
              <select value={mode} onChange={(e) => { setMode(e.target.value); setAuthorized(false); }} className={`${inputClass} !mt-2 !py-3`}>
                <option value="quick">Quick</option><option value="standard">Standard</option><option value="deep">Deep</option>
              </select>
            </label>
            <label className="block text-sm text-[#aaa]">LLM budget (USD)
              <input required type="number" min={unlimitedBudget ? -1 : 0.01} step="0.01" value={budget} aria-describedby="budget-help" onChange={(e) => {
                setBudget(e.target.value);
                setAuthorized(false);
                const value = e.target.valueAsNumber;
                e.target.setCustomValidity(value === -1 || value > 0 ? "" : "Enter a positive USD amount, or -1 for unlimited.");
              }} className={`${inputClass} !mt-2 !py-3`} />
              <span id="budget-help" className="mt-1 block text-xs text-[#888]">
                {unlimitedBudget ? "Unlimited: no LLM spending cap." : "Use -1 for unlimited (no LLM spending cap)."}
              </span>
            </label>
          </div>
          <details className="group rounded-lg border border-[#333]">
            <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-4 py-4 text-sm text-[#ccc] hover:text-white focus-visible:outline-emerald-400"><ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" aria-hidden="true" />Instructions <span className="text-[#888]">(optional)</span></summary>
            <label className="block px-4 pb-4"><span className="sr-only">Instructions</span><textarea maxLength={16000} rows={3} value={instruction} onChange={(e) => { setInstruction(e.target.value); setAuthorized(false); }} placeholder="Focus on authentication and authorization" className={inputClass} /></label>
          </details>
          <p className="break-words border-t border-[#292929] pt-5 text-xs text-[#888]">{llm ?? "Loading LLM profile…"} <span aria-hidden="true">·</span> E2B</p>
          {(runnerCheckError || (checked && imageBlocked)) && <div className="space-y-3">
            {runnerCheckError ? <p role="alert" className="text-sm text-red-400">{runnerCheckError}</p> : <RunnerCheckStatus check={checked} />}
            <div className="flex gap-4"><button type="button" className={buttonClass} onClick={() => setRunnerCheckAttempt(value => value + 1)}>Retry E2B check</button><a href="#page=settings&section=e2b" className="self-center text-xs text-emerald-400 underline underline-offset-4">Configure E2B</a></div>
          </div>}
          {active && <p role="alert" className="text-sm text-red-400">E2B concurrency limit reached. Stop a scan or wait for a slot.</p>}
          {(error || pollError) && <p role="alert" className="text-sm text-red-400">{error || pollError}</p>}
          <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
            <input required type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
            <span>{unlimitedBudget
              ? "I am authorized to test this target and approve LLM usage without a spending cap."
              : "I am authorized to test this target and approve the configured LLM usage."}
              <span className="mt-1 block text-[#888]">E2B compute is billed separately.</span>
            </span>
          </label>
          <div className="flex justify-end gap-3">
            <button type="button" className={`${buttonClass} !px-5 !py-3`} onClick={() => onCreatingChange(false)}>Cancel</button>
            <button disabled={!authorized || active || imageBlocked || sourceBlocked || (source?.kind === "zip" && !source.upload_id)} aria-describedby={repositoryId && repositoryRef ? "github-source-status" : undefined} type="submit" className={`${buttonClass} inline-flex items-center gap-2 bg-white !px-5 !py-3 !text-sm font-medium !text-black hover:bg-[#ddd]`}>
              {pending || (!checked && !runnerCheckError) ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}{pending ? source?.kind === "github" ? "Preparing repository…" : "Starting…" : !checked && !runnerCheckError ? "Checking E2B…" : "Start scan"}
            </button>
          </div>
        </fieldset>
      </form>
  </div>;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Pentest</h1>
          <p className="mt-1 text-sm text-[#888]">Create scans, review findings, and manage local scan history.</p>
        </div>
        <button id="new-scan-trigger" disabled={pending} className={`${buttonClass} inline-flex items-center gap-2 bg-white !text-black hover:bg-[#ddd]`} onClick={() => { onCreatingChange(true); setAuthorized(false); setSource(null); }}><Plus className="h-4 w-4" aria-hidden="true" />New scan</button>
      </div>
      {((error && !rerunId && !resumeId) || pollError) && <p role="alert" className="text-sm text-red-400">{pollError || error}</p>}

      <section aria-label="Scan jobs" className="space-y-3">
        <h2 className="text-sm font-semibold">Dashboard scans</h2>
        {jobs === null ? <p role="status" className="text-sm text-[#888]">Loading scans…</p> : jobs.length === 0 ? <p className="text-sm text-[#888]">No dashboard scans yet. Use New scan to create one.</p> : jobs.map((job) => {
          const SourceIcon = job.source?.kind === "github" ? GitBranch : job.source?.kind === "zip" ? FileArchive : Globe;
          const sourceLabel = job.source?.kind === "github" ? "Repository scan" : job.source?.kind === "zip" ? "ZIP scan" : "Website scan";
          const target = job.source?.kind === "github" ? job.source.repository : job.source?.kind === "zip" ? job.source.filename : job.target;
          const status = job.status === "running" ? job.remote?.phase || "Running" : job.status;
          return <article key={job.id} aria-label={`${sourceLabel}: ${target}`} className={`relative rounded-xl border border-[#222] bg-white/[0.02] p-4 transition-colors ${job.has_run ? "hover:border-[#444] hover:bg-white/[0.04]" : ""}`}>
            {job.has_run && <a href={`#page=pentest&run=${encodeURIComponent(job.id)}`} aria-label={`Open scan ${target}`} className="absolute inset-0 rounded-xl focus-visible:outline-2 focus-visible:outline-emerald-400" onClick={event => { event.preventDefault(); onSelectRun(job.id); }} />}
            <div className="pointer-events-none flex items-center gap-3">
              <SourceIcon className="h-5 w-5 shrink-0 text-[#888]" role="img" aria-label={sourceLabel} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{target}</p>
                <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-[#888]"><span className={`capitalize ${ACTIVE.has(job.status) ? "text-emerald-400" : job.status === "failed" ? "text-red-400" : ""}`}>{status}</span><span aria-hidden="true">·</span><span className="capitalize">{job.scan_mode}</span><span aria-hidden="true">·</span><span>{job.max_budget_usd === -1 ? "Unlimited LLM budget" : `$${job.max_budget_usd} LLM budget`}</span>{job.source?.kind === "github" && <><span aria-hidden="true">·</span><span className="truncate">{job.source.ref}{job.source.commit && ` · ${job.source.commit.slice(0, 7)}`}</span></>}</p>
              </div>
              <div className="pointer-events-auto relative z-10 flex shrink-0 gap-2">
                {(job.remote?.cleanup === "failed" || job.remote?.results_pending) && job.runner === "e2b" && !ACTIVE.has(job.status) && <ScanAction label={job.remote?.results_pending ? "Retry results" : "Retry cleanup"} disabled={pending} onClick={() => void stop(job.id)}><RotateCcw className="h-4 w-4" aria-hidden="true" /></ScanAction>}
                {job.can_resume && <ScanAction label="Resume scan" disabled={pending || active} aria-expanded={resumeId === job.id} aria-controls={`resume-${job.id}`} onClick={() => { setResumeId(resumeId === job.id ? null : job.id); setResumeAuthorized(false); setResumeBudget(String(job.max_budget_usd)); setRerunId(null); setError(null); }}><Play className="h-4 w-4" aria-hidden="true" /></ScanAction>}
                {job.status === "failed" && job.remote?.cleanup !== "failed" && !job.remote?.results_pending && <ScanAction label="Rerun scan" disabled={pending || active} aria-expanded={rerunId === job.id} aria-controls={`rerun-${job.id}`} onClick={() => { setRerunId(rerunId === job.id ? null : job.id); setRerunAuthorized(false); setResumeId(null); setError(null); }}><RotateCcw className="h-4 w-4" aria-hidden="true" /></ScanAction>}
                {ACTIVE.has(job.status) && <ScanAction label={job.status === "stopping" ? "Stopping scan…" : "Stop scan"} className="!text-red-400" disabled={pending || job.status === "stopping"} onClick={() => void stop(job.id)}><Square className="h-4 w-4" aria-hidden="true" /></ScanAction>}
                <ScanAction label="Delete scan" className="!text-red-400" disabled={pending || deleting || ACTIVE.has(job.status) || !!job.remote?.results_pending || (!!job.remote && !["paused", "archived", "deleted", "not_created"].includes(job.remote.cleanup))} onClick={() => void onDeleteRun(job.id)}><Trash2 className="h-4 w-4" aria-hidden="true" /></ScanAction>
              </div>
            </div>
            {job.status === "failed" ? <ScanFailure job={job} /> : job.error && <p role="alert" className="pointer-events-none mt-3 text-xs text-red-400">{job.error}</p>}
            {job.status !== "failed" && !ACTIVE.has(job.status) && !job.error && job.remote?.sync_error && <p role="alert" className="pointer-events-none mt-3 text-xs text-red-400">{job.remote.sync_error}</p>}
            {job.can_resume && resumeId === job.id && <div id={`resume-${job.id}`} className="relative z-10 mt-4 space-y-3 rounded-lg border border-emerald-500/30 bg-[#07120c] p-4">
              <p className="text-sm font-medium">Resume stopped scan?</p>
              <p className="text-xs leading-relaxed text-[#aaa]">Continues this same run from its saved agent checkpoint, preserving conversation history and findings. {job.remote ? "Restores the retained sandbox; it does not create a new VM. Provider compute charges resume." : "Recreates the local tool sandbox; previous running commands are not restored."} Uses the currently selected LLM: {llm ?? "Loading…"}.</p>
              {job.source && <p className="text-xs text-emerald-400">Uses the same source snapshot, not the latest GitHub branch or a new upload.</p>}
              <label className="block text-xs text-[#aaa]">Total LLM budget (USD)
                <input type="number" step="any" value={resumeBudget} onChange={e => { setResumeBudget(e.target.value); setResumeAuthorized(false); setError(null); }} disabled={pending} aria-describedby={`resume-budget-${job.id}`} className={inputClass} />
              </label>
              <p id={`resume-budget-${job.id}`} className="text-xs text-[#888]">Includes spending before the stop; this is not an additional allowance. Increase it if exhausted, or use -1 for unlimited. In-flight requests may exceed the limit.</p>
              <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
                <input type="checkbox" checked={resumeAuthorized} disabled={pending || active} onChange={e => setResumeAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
                I am authorized to continue testing this target and approve the total LLM budget above{job.remote ? " and resumed provider compute charges" : ""}.
              </label>
              <div className="flex flex-wrap gap-2">
                <button className={`${buttonClass} bg-white !text-black hover:bg-[#ddd]`} disabled={!resumeAuthorized || pending || active || !llm || !Number.isFinite(Number(resumeBudget)) || (Number(resumeBudget) !== -1 && Number(resumeBudget) <= 0)} onClick={() => void resume(job.id)}>{pending ? "Resuming…" : "Confirm resume"}</button>
                <button className={buttonClass} disabled={pending} onClick={() => { setResumeId(null); setResumeAuthorized(false); setError(null); }}>Cancel</button>
              </div>
              {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
            </div>}
            {job.status === "failed" && rerunId === job.id && <div id={`rerun-${job.id}`} className="relative z-10 mt-4 space-y-3 rounded-lg border border-emerald-500/30 bg-[#07120c] p-4">
              <p className="text-sm font-medium">Rerun failed scan?</p>
              {job.source && <p className="text-xs text-emerald-400">Uses the exact same source snapshot, in a new working copy.</p>}
              <p className="text-xs leading-relaxed text-[#aaa]">Creates a new E2B job with the same target, scan mode, LLM budget, and instructions. E2B compute is billed separately. The failed attempt stays unchanged. Uses the currently selected LLM: {llm ?? "Loading…"}.</p>
              <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
                <input type="checkbox" checked={rerunAuthorized} disabled={pending || active} onChange={e => setRerunAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
                {job.max_budget_usd === -1
                  ? "I am authorized to retest this target and approve LLM usage without a spending cap."
                  : `I am authorized to retest this target and approve LLM usage up to $${job.max_budget_usd}. In-flight requests may exceed the limit.`}
              </label>
              <div className="flex flex-wrap gap-2">
                <button className={`${buttonClass} bg-white !text-black hover:bg-[#ddd]`} disabled={!rerunAuthorized || pending || active || !llm} onClick={() => void rerun(job.id)}>{pending ? "Starting…" : "Confirm rerun"}</button>
                <button className={buttonClass} disabled={pending} onClick={() => { setRerunId(null); setRerunAuthorized(false); setError(null); }}>Cancel</button>
              </div>
              {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
            </div>}
          </article>;
        })}
      </section>
    </div>
  );
}
