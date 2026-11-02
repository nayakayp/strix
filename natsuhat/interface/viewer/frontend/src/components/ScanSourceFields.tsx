import { useEffect, useRef, useState } from "react";
import { fetchGithub, uploadSource, type GithubRepository, type ScanSource, type SourceSnapshot } from "@/data/serverSource";

const inputClass = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";

export default function ScanSourceFields({ target, onTargetChange, source, onChange, runner, githubCheck, onCheckAgain }: {
  target: string;
  onTargetChange: (value: string) => void;
  source: ScanSource | null;
  onChange: (value: ScanSource | null) => void;
  runner: string;
  githubCheck: { commit?: string; error?: string } | null;
  onCheckAgain: () => void;
}) {
  const kind = source?.kind ?? "target";
  const [repositories, setRepositories] = useState<GithubRepository[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<SourceSnapshot | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const upload = useRef<AbortController | null>(null);
  useEffect(() => () => upload.current?.abort(), []);
  useEffect(() => {
    if (kind !== "github") return;
    let cancelled = false;
    setLoading(true);
    fetchGithub().then(state => { if (!cancelled) setRepositories(state.repositories); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [kind]);

  async function selectFile(file?: File) {
    if (!file) return;
    upload.current?.abort();
    setError(null); setSnapshot(null);
    onChange({ kind: "zip", upload_id: "" });
    if (!file.name.toLowerCase().endsWith(".zip") || file.size === 0 || file.size > 100 * 1024 * 1024) {
      setProgress(null); setError("Choose a non-empty .zip file up to 100 MiB."); return;
    }
    const controller = new AbortController();
    upload.current = controller;
    setProgress(0);
    try {
      const result = await uploadSource(file, setProgress, controller.signal);
      if (!controller.signal.aborted) { setSnapshot(result); onChange({ kind: "zip", upload_id: result.upload_id! }); }
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not upload ZIP.");
    } finally { if (upload.current === controller) setProgress(null); }
  }

  return <div className="space-y-3">
    <label className="block text-sm text-[#aaa]">Source
      <select className={inputClass} value={kind} onChange={event => {
        upload.current?.abort(); setProgress(null); setError(null); setSnapshot(null);
        const value = event.target.value;
        onChange(value === "target" ? null : value === "zip" ? { kind: "zip", upload_id: "" } : { kind: "github", repository_id: 0, ref: "" });
      }}>
        <option value="target">Target URL or server path</option>
        <option value="github">Connected GitHub repository</option>
        <option value="zip">Upload ZIP codebase</option>
      </select>
    </label>
    {!source && <label className="block text-sm text-[#aaa]">Target
      <input required maxLength={4096} value={target} onChange={e => onTargetChange(e.target.value)} placeholder={runner === "local" ? "https://staging.example.com or /path/on/server" : "https://staging.example.com"} className={inputClass} />
    </label>}
    {source?.kind === "github" && <>
      {loading ? <p role="status" className="text-xs text-[#888]">Loading connected repositories…</p> : repositories.length === 0 ? <p className="text-sm text-[#aaa]">No connected repositories. <a className="text-emerald-400 underline" href="#page=integrations">Connect a repository in Integrations</a>.</p> : <>
        <label className="block text-sm text-[#aaa]">Repository
          <select required className={inputClass} value={source.repository_id || ""} onChange={e => {
            const repository = repositories.find(r => r.id === Number(e.target.value));
            onChange({ kind: "github", repository_id: repository?.id ?? 0, ref: repository?.default_branch ?? "" });
          }}>
            <option value="">Choose a connected repository</option>
            {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.full_name}{repo.private ? " · Private" : ""}</option>)}
          </select>
        </label>
        <label className="block text-sm text-[#aaa]">Branch, tag, or commit
          <input required maxLength={300} className={inputClass} value={source.ref} onChange={e => onChange({ ...source, ref: e.target.value })} placeholder="main" />
        </label>
        <p className="text-xs text-[#888]">Resolved to an exact commit before scanning. Reruns use that same snapshot, not the latest branch.</p>
        {!!source.repository_id && !!source.ref.trim() && <div id="github-source-status" className="space-y-2">
          {githubCheck?.error ? <p role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-300">{githubCheck.error}</p>
            : githubCheck?.commit ? <p role="status" className="text-xs text-emerald-400">Repository ready · commit {githubCheck.commit.slice(0, 12)}</p>
            : <p role="status" className="text-xs text-[#888]">Checking repository and selected branch, tag, or commit…</p>}
          <button type="button" disabled={!githubCheck} onClick={onCheckAgain} className="cursor-pointer rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40">Check again</button>
          <p className="text-xs text-[#888]">Checks GitHub access and commit availability only. Does not download source or start a scan.</p>
        </div>}
      </>}
    </>}
    {source?.kind === "zip" && <>
      <label className="block rounded-lg border border-dashed border-[#444] bg-white/[0.02] p-5 text-sm text-[#aaa]" onDragOver={e => e.preventDefault()} onDrop={e => {
        e.preventDefault();
        if (!e.currentTarget.closest("fieldset")?.disabled) void selectFile(e.dataTransfer.files[0]);
      }}>
        <span className="block font-medium text-white">Drop a ZIP codebase here or choose a file</span>
        <span className="mt-1 block text-xs text-[#888]">No GitHub account or Git history required. 100 MiB ZIP · 500 MiB extracted · 20,000 entries.</span>
        <input aria-label="ZIP codebase" type="file" accept=".zip,application/zip" className="mt-3 w-full text-xs text-transparent file:mr-3 file:rounded file:border file:border-[#444] file:bg-[#111] file:px-3 file:py-2 file:text-white" onChange={e => { void selectFile(e.target.files?.[0]); e.target.value = ""; }} />
      </label>
      {progress !== null && <div role="status" className="space-y-2 text-xs text-emerald-400">
        <p>{progress === 100 ? "Validating source files…" : `Uploading… ${progress}%`}</p>
        <progress aria-label="ZIP upload progress" value={progress} max={100} className="h-1 w-full accent-emerald-500" />
        <button type="button" className="underline" onClick={() => { upload.current?.abort(); setProgress(null); }}>Cancel upload</button>
      </div>}
      {snapshot && <div role="status" className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 text-xs text-emerald-400">
        <p className="break-all">Ready: {snapshot.filename} · {snapshot.files.toLocaleString()} files · {(snapshot.size / 1024 / 1024).toFixed(2)} MiB extracted</p>
        {snapshot.warnings.map(warning => <p key={warning} className="mt-2 text-amber-400">{warning}</p>)}
      </div>}
      <p className="text-xs text-[#888]">Uploads expire after 24 hours unless used for a scan. Uploading does not start a scan. Git metadata and macOS archive metadata are excluded.</p>
    </>}
    {source && <p className="text-xs leading-relaxed text-amber-400">Source may contain secrets. Review the files before submitting. {runner === "local" ? "Only an isolated working copy is mounted into Docker." : "The code is uploaded to the selected provider and remains in its retained sandbox."} Code excerpts may be sent to your selected LLM provider during scanning.</p>}
    {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
  </div>;
}
