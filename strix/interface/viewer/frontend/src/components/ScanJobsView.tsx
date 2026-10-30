import { useEffect, useState, type FormEvent } from "react";
import { Play, RotateCcw, Square, Terminal } from "lucide-react";
import { fetchJobs, fetchJobLog, fetchLlmSettings, rerunJob, startJob, stopJob, type ScanJob } from "@/data/serverSource";

const ACTIVE = new Set(["starting", "running", "stopping"]);
const inputClass = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const buttonClass = "cursor-pointer rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export default function ScanJobsView({ onSelectRun }: { onSelectRun: (name: string) => void }) {
  const [jobs, setJobs] = useState<ScanJob[] | null>(null);
  const [target, setTarget] = useState("");
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
  const active = jobs?.some((job) => ACTIVE.has(job.status)) ?? false;
  const unlimitedBudget = Number(budget) === -1;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [list, config] = await Promise.all([fetchJobs(), fetchLlmSettings()]);
        const provider = config.providers.find(p => p.id === config.selected_id);
        const text = logJob ? await fetchJobLog(logJob) : null;
        if (!cancelled) {
          setJobs(list);
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
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await startJob({ target, scan_mode: mode, max_budget_usd: Number(budget), instruction, authorized });
      setAuthorized(false);
      setJobs(await fetchJobs());
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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Local scans</h1>
        <p className="mt-1 text-sm text-[#888]">One scan at a time, on this server. Results stay in its strix_runs directory.</p>
      </div>
      <form onSubmit={submit} className="space-y-4 rounded-xl border border-[#222] bg-white/[0.02] p-5">
        <h2 className="text-sm font-semibold">Start a pentest</h2>
        <p className="text-xs leading-relaxed text-[#888]">Requires Docker and a configured LLM on the server. Uses your selected LLM profile, or existing CLI settings; LLM requests can incur charges. Local source directories are mounted writable.</p>
        {llm && <p className="break-all text-xs text-emerald-400">LLM: {llm}</p>}
        <fieldset disabled={pending || active || jobs === null} className="space-y-4">
          <label className="block text-sm text-[#aaa]">Target
            <input required maxLength={4096} value={target} onChange={(e) => setTarget(e.target.value)} placeholder="https://staging.example.com or /path/on/server" className={inputClass} />
          </label>
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
          <button disabled={!authorized} type="submit" className={`${buttonClass} inline-flex items-center gap-2 bg-white !text-black hover:bg-[#ddd]`}>
            <Play className="h-3.5 w-3.5" aria-hidden="true" />{pending ? "Starting…" : "Start local scan"}
          </button>
        </fieldset>
        {active && <p role="status" className="text-xs text-amber-400">A scan is active. Stop it or wait for it to finish before starting another.</p>}
        {((error && !rerunId) || pollError) && <p role="alert" className="text-sm text-red-400">{pollError || error}</p>}
      </form>

      <section aria-label="Scan jobs" className="space-y-3">
        <h2 className="text-sm font-semibold">Jobs</h2>
        {jobs === null ? <p role="status" className="text-sm text-[#888]">Loading jobs…</p> : jobs.length === 0 ? <p className="text-sm text-[#888]">No dashboard-launched scans yet. Previous CLI runs are in Past runs.</p> : jobs.map((job) => (
          <div key={job.id} className="rounded-xl border border-[#222] bg-white/[0.02] p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="break-all text-sm font-medium">{job.target}</p>
                <p className="mt-1 text-xs text-[#888]"><span className={ACTIVE.has(job.status) ? "text-emerald-400 capitalize" : "capitalize"}>{job.status}</span> · {job.scan_mode} · {job.max_budget_usd === -1 ? "Unlimited LLM budget" : `$${job.max_budget_usd} budget`}</p>
                <p className="mt-1 break-all font-mono text-[10px] text-[#666]">{job.id}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {job.has_run && <button className={buttonClass} onClick={() => onSelectRun(job.id)}>View run</button>}
                <button className={`${buttonClass} inline-flex items-center gap-1.5`} aria-expanded={logJob === job.id} onClick={() => { setLog(null); setLogJob(logJob === job.id ? null : job.id); }}><Terminal className="h-3.5 w-3.5" aria-hidden="true" />{logJob === job.id ? "Hide log" : "Show log"}</button>
                {job.status === "failed" && <button className={`${buttonClass} inline-flex items-center gap-1.5`} disabled={pending || active} aria-expanded={rerunId === job.id} aria-controls={`rerun-${job.id}`} onClick={() => { setRerunId(rerunId === job.id ? null : job.id); setRerunAuthorized(false); setError(null); }}><RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />Rerun</button>}
                {ACTIVE.has(job.status) && <button className={`${buttonClass} inline-flex items-center gap-1.5 !text-red-400`} disabled={pending || job.status === "stopping"} onClick={() => void stop(job.id)}><Square className="h-3 w-3" aria-hidden="true" />{job.status === "stopping" ? "Stopping…" : "Stop scan"}</button>}
              </div>
            </div>
            {job.llm_model && <p className="mt-2 break-all text-xs text-[#888]">LLM: {job.llm_model}</p>}
            {job.error && <p role="alert" className="mt-3 text-xs text-red-400">{job.error}</p>}
            {job.status === "failed" && rerunId === job.id && <div id={`rerun-${job.id}`} className="mt-4 space-y-3 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.04] p-4">
              <p className="text-sm font-medium">Rerun failed scan?</p>
              <p className="text-xs leading-relaxed text-[#aaa]">Creates a new job with the same target, scan mode, LLM budget, and instructions. The failed attempt stays unchanged. Uses the currently selected LLM: {llm ?? "Loading…"}.</p>
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
            {logJob === job.id && <pre aria-label="Job log" className="mt-4 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-[#222] bg-black p-3 font-mono text-xs text-[#aaa]">{log === null ? "Loading log…" : log || "No output yet."}</pre>}
          </div>
        ))}
      </section>
    </div>
  );
}
