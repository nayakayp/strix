import { useEffect, useState, type FormEvent } from "react";
import { Play, Plus, RotateCcw, Square, Terminal, Trash2 } from "lucide-react";
import { checkGithubSource, checkRunner, fetchJobs, fetchJobState, fetchJobLog, fetchLlmSettings, rerunJob, resumeJob, startJob, stopJob, type ScanJob, type JobsPayload, type ScanSource, type RunnerCheck } from "@/data/serverSource";
import ScanSourceFields from "@/components/ScanSourceFields";
import { RunnerCheckStatus } from "@/components/RunnerSettingsView";

const ACTIVE = new Set(["starting", "running", "stopping"]);
const inputClass = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const buttonClass = "cursor-pointer rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export default function ScanJobsView({ onSelectRun, onDeleteRun, deleting }: {
  onSelectRun: (name: string) => void;
  onDeleteRun: (name: string) => Promise<void>;
  deleting: boolean;
}) {
  const [jobs, setJobs] = useState<ScanJob[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [target, setTarget] = useState("");
  const [source, setSource] = useState<ScanSource | null>(null);
  const [mode, setMode] = useState("quick");
  const [budget, setBudget] = useState("10");
  const [instruction, setInstruction] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [logJob, setLogJob] = useState<string | null>(null);
  const [log, setLog] = useState<string | null>(null);
  const [llm, setLlm] = useState<string | null>(null);
  const [rerunId, setRerunId] = useState<string | null>(null);
  const [rerunAuthorized, setRerunAuthorized] = useState(false);
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [resumeAuthorized, setResumeAuthorized] = useState(false);
  const [resumeBudget, setResumeBudget] = useState("");
  const [runner, setRunner] = useState("");
  const [configuration, setConfiguration] = useState<JobsPayload | null>(null);
  const [runnerCheck, setRunnerCheck] = useState<{ provider: string; result: RunnerCheck } | null>(null);
  const [runnerCheckError, setRunnerCheckError] = useState<string | null>(null);
  const [githubCheck, setGithubCheck] = useState<{ repository_id: number; ref: string; commit?: string; error?: string } | null>(null);
  const [githubCheckAttempt, setGithubCheckAttempt] = useState(0);
  const repositoryId = source?.kind === "github" ? source.repository_id : 0;
  const repositoryRef = source?.kind === "github" ? source.ref.trim() : "";
  const currentGithubCheck = githubCheck?.repository_id === repositoryId && githubCheck.ref === repositoryRef ? githubCheck : null;
  const sourceBlocked = source?.kind === "github" && !currentGithubCheck?.commit;
  const checked = runnerCheck?.provider === runner ? runnerCheck.result : null;
  const imageBlocked = runner !== "local" && (!checked || checked.availability !== "available" || checked.compatibility === "incompatible");

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
    if (creating && runner && runner !== "local") {
      checkRunner(runner).then(result => { if (!cancelled) setRunnerCheck({ provider: runner, result }); }).catch(e => { if (!cancelled) setRunnerCheckError(e.message); });
    }
    return () => { cancelled = true; };
  }, [creating, runner]);

  function atCapacity(selected: string) {
    const running = jobs?.filter(job => ACTIVE.has(job.status)) ?? [];
    return selected === "local"
      ? running.some(job => (job.runner ?? "local") === "local")
      : running.filter(job => job.runner && job.runner !== "local").length >= (configuration?.max_remote_concurrent ?? 0);
  }
  const active = atCapacity(runner);
  const unlimitedBudget = Number(budget) === -1;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [state, config] = await Promise.all([fetchJobState(), fetchLlmSettings()]);
        const provider = config.providers.find(p => p.id === config.selected_id);
        const hasLog = logJob && state.jobs.some(job => job.id === logJob);
        const text = hasLog ? await fetchJobLog(logJob) : null;
        if (!cancelled) {
          if (logJob && !hasLog) setLogJob(null);
          setJobs(state.jobs);
          setConfiguration(state);
          setRunner(current => current || state.default_runner);
          setLlm(provider ? `${provider.name} · ${provider.model}` : "Server CLI settings");
          setLog(text);
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
  }, [logJob]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending || sourceBlocked) return;
    setPending(true);
    setError(null);
    try {
      await startJob({ target, ...(source ? { source } : {}), runner, scan_mode: mode, max_budget_usd: Number(budget), instruction, authorized });
      setAuthorized(false);
      setJobs(await fetchJobs());
      setCreating(false);
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

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Pentest</h1>
          <p className="mt-1 text-sm text-[#888]">Create scans, review findings, and manage local scan history.</p>
        </div>
        <button disabled={pending} className={`${buttonClass} inline-flex items-center gap-2 bg-white !text-black hover:bg-[#ddd]`} aria-expanded={creating} aria-controls="create-scan" onClick={() => { setCreating(!creating); setAuthorized(false); setSource(null); }}><Plus className="h-4 w-4" aria-hidden="true" />{creating ? "Cancel" : "New scan"}</button>
      </div>
      {creating && <form id="create-scan" onSubmit={submit} className="space-y-4 rounded-xl border border-[#222] bg-white/[0.02] p-5">
        <h2 className="text-sm font-semibold">New scan</h2>
        <p className="text-xs text-[#888]">One local scan and up to {configuration?.max_remote_concurrent ?? "…"} concurrent remote scans. Each remote scan gets its own sandbox; results sync here.</p>
        <p className="text-xs leading-relaxed text-[#888]">Uses your selected LLM profile, or server CLI settings. The LLM budget does not cover sandbox compute charges. Only submit targets you are authorized to test.</p>
        {llm && <p className="break-all text-xs text-emerald-400">LLM: {llm}</p>}
        <fieldset disabled={pending || jobs === null} className="space-y-4">
          <label className="block text-sm text-[#aaa]">Runner
            <select value={runner} onChange={e => { setRunner(e.target.value); setAuthorized(false); }} className={inputClass}>
              {(configuration?.runners ?? [{ id: "local", name: "Local Docker", configured: true }]).map(item => <option key={item.id} value={item.id} disabled={!item.configured}>{item.name}{!item.configured ? " — API key not configured" : ""}</option>)}
            </select>
          </label>
          <p className="text-xs leading-relaxed text-[#888]">{!runner || runner === "local"
            ? "Runs on this server and requires Docker. Local source directories are mounted writable."
            : `Creates a fresh ${runner === "e2b" ? "E2B" : "Daytona"} sandbox from a scan-ready image with Natsuhat, dependencies, and security tools preinstalled. No per-scan installation or tool build. Total time limit: ${Math.round((configuration?.remote_timeout_seconds ?? 14400) / 60)} minutes, including provisioning. The selected LLM connection is sent to the provider; its API key stays out of scan results. Local server paths are not uploaded.`}</p>
          {runner === "e2b" && <p className="text-xs leading-relaxed text-[#888]">E2B sessions pause after at most one hour; this dashboard resumes the same VM while the scan is active. Finished or stopped sandboxes stay paused with their data, without traffic-triggered auto-resume.</p>}
          {runner === "daytona" && <p className="text-xs text-amber-400">Daytona must allow outbound access to your model provider and the target. Restricted organization tiers may block these destinations.</p>}
          {runner && runner !== "local" && <div className="space-y-2">
            {checked ? <><p className="break-all text-xs text-[#aaa]">Prepared image: <span className="font-mono">{checked.template}</span></p><RunnerCheckStatus check={checked} /></> : <p role="status" className="text-xs text-amber-300">{runnerCheckError ?? "Checking prepared image availability before scanning…"}</p>}
            <a href="#page=runners" className="text-xs text-emerald-400 underline underline-offset-4">Configure sandbox runners</a>
          </div>}
          <ScanSourceFields target={target} onTargetChange={value => { setTarget(value); setAuthorized(false); }} source={source} onChange={value => { setSource(value); setAuthorized(false); }} runner={runner} githubCheck={currentGithubCheck} onCheckAgain={() => { setGithubCheck(null); setGithubCheckAttempt(value => value + 1); }} />
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm text-[#aaa]">Scan mode
              <select value={mode} onChange={(e) => setMode(e.target.value)} className={inputClass}>
                <option value="quick">Quick</option><option value="standard">Standard</option><option value="deep">Deep</option>
              </select>
            </label>
            <label className="block text-sm text-[#aaa]">LLM budget (USD)
              <input required type="number" min="-1" step="0.01" value={budget} aria-describedby="budget-help" onChange={(e) => {
                setBudget(e.target.value);
                const value = e.target.valueAsNumber;
                e.target.setCustomValidity(value === -1 || value > 0 ? "" : "Enter a positive USD amount, or -1 for unlimited.");
              }} className={inputClass} />
              <span id="budget-help" className={`mt-1 block text-xs ${unlimitedBudget ? "text-amber-400" : "text-[#888]"}`}>
                {unlimitedBudget ? "Unlimited: no LLM spending cap." : "Use -1 for unlimited (no LLM spending cap)."}
              </span>
            </label>
          </div>
          <label className="block text-sm text-[#aaa]">Instructions <span className="text-[#666]">(optional)</span>
            <textarea maxLength={16000} rows={2} value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="Focus on authentication and authorization" className={inputClass} />
          </label>
          <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
            <input required type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
            {unlimitedBudget
              ? "I am authorized to test this target and approve LLM usage without a spending cap."
              : "I am authorized to test this target and approve the configured LLM usage up to this budget. In-flight requests may exceed the limit."}
          </label>
          {runner && runner !== "local" && <p className="text-xs text-amber-400">Submitting also approves {runner === "e2b" ? "E2B" : "Daytona"} compute charges until completion, cancellation, or the time limit. Sandboxes are retained: {runner === "e2b" ? "paused in E2B" : "stopped and archived in Daytona"}.</p>}
          <button disabled={!authorized || active || imageBlocked || sourceBlocked || (source?.kind === "zip" && !source.upload_id)} aria-describedby={repositoryId && repositoryRef ? "github-source-status" : undefined} type="submit" className={`${buttonClass} inline-flex items-center gap-2 bg-white !text-black hover:bg-[#ddd]`}>
            <Play className="h-3.5 w-3.5" aria-hidden="true" />{pending ? source?.kind === "github" ? "Preparing repository…" : "Starting…" : "Start scan"}
          </button>
        </fieldset>
        {active && <p role="status" className="text-xs text-amber-400">{runner === "local" ? "A local scan is active. Choose a remote runner or wait for it to finish." : "Remote concurrency limit reached. Stop a scan or wait for a slot."}</p>}
      </form>}
      {((error && !rerunId && !resumeId) || pollError) && <p role="alert" className="text-sm text-red-400">{pollError || error}</p>}

      <section aria-label="Scan jobs" className="space-y-3">
        <h2 className="text-sm font-semibold">Dashboard scans</h2>
        {jobs === null ? <p role="status" className="text-sm text-[#888]">Loading scans…</p> : jobs.length === 0 ? <p className="text-sm text-[#888]">No dashboard scans yet. Use New scan to create one.</p> : jobs.map((job) => (
          <div key={job.id} className="rounded-xl border border-[#222] bg-white/[0.02] p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 flex-1">
                <p className="break-all text-sm font-medium">{job.target}</p>
                {job.source && <div className="mt-2 space-y-1 text-xs text-[#888]">
                  <p className="break-words">{job.source.kind === "github" ? `GitHub · ${job.source.ref} · ${job.source.commit}` : `ZIP · ${job.source.filename}`} · {job.source.files.toLocaleString()} files</p>
                  <p className="break-all font-mono text-[10px]">Snapshot SHA-256: {job.source.sha256}</p>
                  {job.source.warnings.map(warning => <p key={warning} className="text-amber-400">{warning}</p>)}
                </div>}
                <p className="mt-1 text-xs text-[#888]"><span className={ACTIVE.has(job.status) ? "text-emerald-400 capitalize" : "capitalize"}>{job.status}</span> · {job.runner ?? "local"} · {job.scan_mode} · {job.max_budget_usd === -1 ? "Unlimited LLM budget" : `$${job.max_budget_usd} LLM budget`}</p>
                <p className="mt-1 break-all font-mono text-[10px] text-[#666]">{job.id}</p>
                {job.remote && <p className="mt-2 text-xs text-emerald-400">{job.remote.phase}{job.remote.cleanup === "deleted" ? " · Sandbox deleted" : job.remote.cleanup === "paused" ? " · Sandbox paused (retained)" : job.remote.cleanup === "archived" ? " · Sandbox archived (retained)" : ""}</p>}
                {!!job.remote?.resume_count && <p className="mt-1 text-xs text-[#888]">E2B session resumed {job.remote.resume_count} {job.remote.resume_count === 1 ? "time" : "times"} without restarting the scan.</p>}
                {job.remote?.sandbox_id && <p className="mt-1 break-all font-mono text-[10px] text-[#888]">Sandbox: {job.remote.sandbox_id}</p>}
                {job.remote?.sync_error && <p role="status" className="mt-2 text-xs text-amber-400">{job.remote.sync_error}</p>}
              </div>
              <div className="flex flex-wrap gap-2">
                {job.has_run && <button className={buttonClass} onClick={() => onSelectRun(job.id)}>View scan</button>}
                <button className={`${buttonClass} inline-flex items-center gap-1.5`} aria-expanded={logJob === job.id} onClick={() => { setLog(null); setLogJob(logJob === job.id ? null : job.id); }}><Terminal className="h-3.5 w-3.5" aria-hidden="true" />{logJob === job.id ? "Hide log" : "Show log"}</button>
                {(job.remote?.cleanup === "failed" || job.remote?.results_pending) && !ACTIVE.has(job.status) && <button className={buttonClass} disabled={pending} onClick={() => void stop(job.id)}>{job.remote?.results_pending ? "Retry results" : "Retry cleanup"}</button>}
                {job.can_resume && <button className={`${buttonClass} inline-flex items-center gap-1.5`} disabled={pending || atCapacity(job.runner ?? "local")} aria-expanded={resumeId === job.id} aria-controls={`resume-${job.id}`} onClick={() => { setResumeId(resumeId === job.id ? null : job.id); setResumeAuthorized(false); setResumeBudget(String(job.max_budget_usd)); setRerunId(null); setError(null); }}><Play className="h-3.5 w-3.5" aria-hidden="true" />Resume scan</button>}
                {job.status === "failed" && job.remote?.cleanup !== "failed" && !job.remote?.results_pending && <button className={`${buttonClass} inline-flex items-center gap-1.5`} disabled={pending || atCapacity(job.runner ?? "local")} aria-expanded={rerunId === job.id} aria-controls={`rerun-${job.id}`} onClick={() => { setRerunId(rerunId === job.id ? null : job.id); setRerunAuthorized(false); setResumeId(null); setError(null); }}><RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />Rerun</button>}
                {ACTIVE.has(job.status) && <button className={`${buttonClass} inline-flex items-center gap-1.5 !text-red-400`} disabled={pending || job.status === "stopping"} onClick={() => void stop(job.id)}><Square className="h-3 w-3" aria-hidden="true" />{job.status === "stopping" ? "Stopping…" : "Stop scan"}</button>}
                <button className={`${buttonClass} inline-flex items-center gap-1.5 !text-red-400`} disabled={pending || deleting || ACTIVE.has(job.status) || !!job.remote?.results_pending || (!!job.remote && !["paused", "archived", "deleted", "not_created"].includes(job.remote.cleanup))} onClick={() => void onDeleteRun(job.id)}><Trash2 className="h-3.5 w-3.5" aria-hidden="true" />Delete scan</button>
              </div>
            </div>
            {job.llm_model && <p className="mt-2 break-all text-xs text-[#888]">LLM: {job.llm_model}</p>}
            {job.remote?.results_pending && <p className="mt-2 text-xs text-amber-400">Results remain in the sandbox. Retry results briefly resumes it to retrieve files and retain it again; it does not start a new scan. Provider compute charges may apply.</p>}
            {job.error && <p role="alert" className="mt-3 text-xs text-red-400">{job.error}</p>}
            {["stopped", "interrupted"].includes(job.status) && !job.can_resume && job.resume_unavailable_reason && <p className="mt-2 text-xs text-amber-400">Cannot resume: {job.resume_unavailable_reason}</p>}
            {job.can_resume && resumeId === job.id && <div id={`resume-${job.id}`} className="mt-4 space-y-3 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.04] p-4">
              <p className="text-sm font-medium">Resume stopped scan?</p>
              <p className="text-xs leading-relaxed text-[#aaa]">Continues this same run from its saved agent checkpoint, preserving conversation history and findings. {job.remote ? "Restores the retained sandbox; it does not create a new VM. Provider compute charges resume." : "Recreates the local tool sandbox; previous running commands are not restored."} Uses the currently selected LLM: {llm ?? "Loading…"}.</p>
              {job.source && <p className="text-xs text-emerald-400">Uses the same source snapshot, not the latest GitHub branch or a new upload.</p>}
              <label className="block text-xs text-[#aaa]">Total LLM budget (USD)
                <input type="number" step="any" value={resumeBudget} onChange={e => { setResumeBudget(e.target.value); setResumeAuthorized(false); setError(null); }} disabled={pending} aria-describedby={`resume-budget-${job.id}`} className={inputClass} />
              </label>
              <p id={`resume-budget-${job.id}`} className="text-xs text-[#888]">Includes spending before the stop; this is not an additional allowance. Increase it if exhausted, or use -1 for unlimited. In-flight requests may exceed the limit.</p>
              <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
                <input type="checkbox" checked={resumeAuthorized} disabled={pending || atCapacity(job.runner ?? "local")} onChange={e => setResumeAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
                I am authorized to continue testing this target and approve the total LLM budget above{job.remote ? " and resumed provider compute charges" : ""}.
              </label>
              <div className="flex flex-wrap gap-2">
                <button className={`${buttonClass} bg-white !text-black hover:bg-[#ddd]`} disabled={!resumeAuthorized || pending || atCapacity(job.runner ?? "local") || !llm || !Number.isFinite(Number(resumeBudget)) || (Number(resumeBudget) !== -1 && Number(resumeBudget) <= 0)} onClick={() => void resume(job.id)}>{pending ? "Resuming…" : "Confirm resume"}</button>
                <button className={buttonClass} disabled={pending} onClick={() => { setResumeId(null); setResumeAuthorized(false); setError(null); }}>Cancel</button>
              </div>
              {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
            </div>}
            {job.status === "failed" && rerunId === job.id && <div id={`rerun-${job.id}`} className="mt-4 space-y-3 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.04] p-4">
              <p className="text-sm font-medium">Rerun failed scan?</p>
              {job.source && <p className="text-xs text-emerald-400">Uses the exact same source snapshot, in a new working copy.</p>}
              <p className="text-xs leading-relaxed text-[#aaa]">Creates a new job with the same target, runner ({job.runner ?? "local"}), scan mode, LLM budget, and instructions. Remote compute is billed separately. The failed attempt stays unchanged. Uses the currently selected LLM: {llm ?? "Loading…"}.</p>
              <label className="flex items-start gap-2 text-xs leading-relaxed text-[#aaa]">
                <input type="checkbox" checked={rerunAuthorized} disabled={pending || atCapacity(job.runner ?? "local")} onChange={e => setRerunAuthorized(e.target.checked)} className="mt-0.5 accent-emerald-500" />
                {job.max_budget_usd === -1
                  ? "I am authorized to retest this target and approve LLM usage without a spending cap."
                  : `I am authorized to retest this target and approve LLM usage up to $${job.max_budget_usd}. In-flight requests may exceed the limit.`}
              </label>
              <div className="flex flex-wrap gap-2">
                <button className={`${buttonClass} bg-white !text-black hover:bg-[#ddd]`} disabled={!rerunAuthorized || pending || atCapacity(job.runner ?? "local") || !llm} onClick={() => void rerun(job.id)}>{pending ? "Starting…" : "Confirm rerun"}</button>
                <button className={buttonClass} disabled={pending} onClick={() => { setRerunId(null); setRerunAuthorized(false); setError(null); }}>Cancel</button>
              </div>
              {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
            </div>}
            {logJob === job.id && <pre aria-label="Job log" className="mt-4 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-[#222] bg-black p-3 font-mono text-xs text-[#aaa]">{log === null ? "Loading log…" : log || "No output yet."}</pre>}
          </div>
        ))}
      </section>
    </div>
  );
}
