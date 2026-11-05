import { useEffect, useId, useRef, useState } from "react";
import { FileArchive, Globe, LoaderCircle, LockKeyhole, Trash2, Upload } from "lucide-react";
import { SiGithub } from "react-icons/si";
import { fetchGithub, uploadSource, type GithubRepository, type ScanSource, type SourceSnapshot } from "@/data/serverSource";

const inputClass = "mt-2 w-full rounded-lg border border-[#333] bg-black px-3 py-3 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const buttonClass = "cursor-pointer rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] hover:border-[#666] hover:text-white focus-visible:outline-emerald-400";

function targetValidationMessage(value: string): string {
  if (!value) return "";
  const target = value.trim();
  try {
    const host = target.includes(":") && !target.includes("://") && !target.startsWith("[") && target.split(":").length > 2 ? `[${target}]` : target;
    const url = new URL(host.includes("://") ? host : `http://${host}`);
    if (target && !/\s/.test(target) && ["http:", "https:"].includes(url.protocol) && url.hostname) return "";
  } catch { /* Display the same field error for malformed hosts and URLs. */ }
  return "Enter a valid URL, domain, or IP address.";
}

export default function ScanSourceFields({ target, onTargetChange, source, onChange, githubCheck, onCheckAgain }: {
  target: string;
  onTargetChange: (value: string) => void;
  source: ScanSource | null;
  onChange: (value: ScanSource | null) => void;
  githubCheck: { commit?: string; error?: string } | null;
  onCheckAgain: () => void;
}) {
  const kind = source?.kind ?? "target";
  const repositoryListId = useId();
  const [repositories, setRepositories] = useState<GithubRepository[] | null>(null);
  const [repositoryAttempt, setRepositoryAttempt] = useState(0);
  const [repositoryError, setRepositoryError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [githubDraft, setGithubDraft] = useState<Extract<ScanSource, { kind: "github" }>>({ kind: "github", repository_id: 0, ref: "" });
  const [error, setError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<SourceSnapshot | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [filename, setFilename] = useState("");
  const [targetError, setTargetError] = useState<string | null>(null);
  const upload = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  useEffect(() => () => upload.current?.abort(), []);
  useEffect(() => {
    if (kind !== "github") return;
    let cancelled = false;
    setRepositoryError(null);
    fetchGithub().then(state => { if (!cancelled) setRepositories(state.repositories); })
      .catch(e => { if (!cancelled) setRepositoryError(e.message); });
    return () => { cancelled = true; };
  }, [kind, repositoryAttempt]);

  function changeGithub(value: Extract<ScanSource, { kind: "github" }>) {
    setGithubDraft(value);
    onChange(value);
  }

  function cancelUpload() {
    upload.current?.abort();
    setProgress(null);
  }

  async function selectFile(file?: File) {
    if (!file) return;
    cancelUpload();
    setError(null); setSnapshot(null); setFilename(file.name);
    onChange({ kind: "zip", upload_id: "" });
    if (!file.name.toLowerCase().endsWith(".zip") || file.size === 0 || file.size > 100 * 1024 * 1024) {
      setError("Choose a non-empty .zip file up to 100 MiB."); return;
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

  return <div className="space-y-7">
    <fieldset>
      <legend className="sr-only">Scan source</legend>
      <div className="grid grid-cols-3 gap-2 sm:gap-4">
        {([{ value: "target", label: "Website", Icon: Globe }, { value: "github", label: "GitHub", Icon: SiGithub }, { value: "zip", label: "ZIP file", Icon: FileArchive }] as const).map(({ value, label, Icon }) => <label key={value} className={`relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border px-2 py-6 transition-colors sm:gap-4 sm:py-8 ${kind === value ? "border-emerald-400 bg-emerald-500/[0.04]" : "border-[#333] hover:border-[#666] hover:bg-white/[0.02]"} has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-emerald-400`}>
          <input type="radio" name="scan-source" value={value} checked={kind === value} className="peer sr-only" onChange={() => {
            cancelUpload(); setError(null);
            onChange(value === "target" ? null : value === "github" ? githubDraft : { kind: "zip", upload_id: snapshot?.upload_id ?? "" });
          }} />
          <span aria-hidden="true" className={`absolute right-3 top-3 h-3.5 w-3.5 rounded-full border ${kind === value ? "border-emerald-400 bg-emerald-400 shadow-[inset_0_0_0_3px_#07110e]" : "border-[#666]"}`} />
          <Icon className="h-7 w-7 sm:h-9 sm:w-9" aria-hidden="true" />
          <span className="text-sm font-medium sm:text-base">{label}</span>
        </label>)}
      </div>
    </fieldset>
    {!source && <div>
      <label className="block text-sm text-[#ccc]">Website target
        <input required maxLength={4096} value={target} ref={input => { input?.setCustomValidity(targetValidationMessage(target)); }} aria-invalid={!!targetError} aria-describedby="website-target-help" onInvalid={event => { event.preventDefault(); setTargetError(targetValidationMessage(target) || "Enter a URL, domain, or IP address."); event.currentTarget.focus(); }} onBlur={() => setTargetError(targetValidationMessage(target) || null)} onChange={event => { setTargetError(null); onTargetChange(event.target.value); }} placeholder="https://staging.example.com" className={inputClass} />
      </label>
      <p id="website-target-help" className={`mt-2 text-xs ${targetError ? "text-red-400" : "text-[#888]"}`} role={targetError ? "alert" : undefined}>{targetError ?? "URL, domain, or IP address."}</p>
    </div>}
    {source?.kind === "github" && <div className="space-y-4">
      {repositoryError ? <div className="space-y-2"><p role="alert" className="text-sm text-red-400">{repositoryError}</p><button type="button" className={buttonClass} onClick={() => { setRepositories(null); setRepositoryAttempt(value => value + 1); }}>Retry loading repositories</button></div>
        : repositories === null ? <p role="status" className="flex items-center gap-2 text-sm text-[#888]"><LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />Loading repositories…</p>
          : repositories.length === 0 ? <div className="rounded-lg border border-[#333] p-5 text-sm"><p className="text-[#aaa]">No connected repositories.</p><a href="#page=integrations" className="mt-3 inline-block text-emerald-400 underline underline-offset-4">Connect GitHub</a></div> : <>
            <label className="block text-sm text-[#ccc]">Repository
              <div className="relative">
                <SiGithub className="pointer-events-none absolute left-3 top-5 h-4 w-4 text-[#aaa]" aria-hidden="true" />
                <input required list={repositoryListId} value={query} autoComplete="off" placeholder="Search connected repositories" className={`${inputClass} !pl-10`} onChange={event => {
                  setQuery(event.target.value);
                  const repository = repositories.find(item => item.full_name === event.target.value);
                  changeGithub({ kind: "github", repository_id: repository?.id ?? 0, ref: repository?.default_branch ?? "" });
                }} />
                <datalist id={repositoryListId}>{repositories.map(repository => <option key={repository.id} value={repository.full_name}>{repository.private ? "Private" : "Public"}</option>)}</datalist>
              </div>
            </label>
            {repositories.find(repository => repository.id === source.repository_id)?.private && <p className="flex items-center gap-1.5 text-xs text-[#888]"><LockKeyhole className="h-3 w-3" aria-hidden="true" />Private repository</p>}
            {!!source.repository_id && <label className="block text-sm text-[#ccc]">Branch, tag, or commit
              <input required maxLength={300} className={inputClass} value={source.ref} onChange={event => changeGithub({ ...source, ref: event.target.value })} placeholder="main" />
            </label>}
            {!!source.repository_id && !!source.ref.trim() && <div id="github-source-status" className="text-xs">
              {githubCheck?.error ? <div className="space-y-2"><p role="alert" className="text-red-400">{githubCheck.error}</p><button type="button" className={buttonClass} onClick={onCheckAgain}>Retry repository check</button></div>
                : githubCheck?.commit ? <p className="text-[#888]">Commit <code>{githubCheck.commit.slice(0, 12)}</code></p>
                  : <p role="status" className="flex items-center gap-2 text-[#888]"><LoaderCircle className="h-3 w-3 animate-spin" aria-hidden="true" />Checking repository…</p>}
            </div>}
          </>}
    </div>}
    {source?.kind === "zip" && <div className="space-y-3">
      <p className="text-sm text-[#ccc]">Codebase</p>
      <input ref={fileInput} tabIndex={-1} aria-label="ZIP codebase" type="file" accept=".zip,application/zip" className="sr-only" onChange={event => { void selectFile(event.target.files?.[0]); event.target.value = ""; }} />
      {snapshot ? <div className="flex flex-wrap items-center gap-3 rounded-lg border border-[#333] p-4">
        <FileArchive className="h-7 w-7 shrink-0 text-[#ccc]" aria-hidden="true" />
        <div className="min-w-0 flex-1"><p className="break-all text-sm font-medium">{snapshot.filename}</p><p className="mt-1 text-xs text-[#888]">{snapshot.files.toLocaleString()} files · {(snapshot.size / 1024 / 1024).toFixed(2)} MiB extracted</p></div>
        <button type="button" className={buttonClass} onClick={() => fileInput.current?.click()}>Replace</button>
        <button type="button" aria-label="Remove ZIP" title="Remove ZIP" className={buttonClass} onClick={() => { setSnapshot(null); setError(null); onChange({ kind: "zip", upload_id: "" }); }}><Trash2 className="h-4 w-4" aria-hidden="true" /></button>
      </div> : progress !== null ? <div role="status" className="space-y-3 rounded-lg border border-[#333] p-4 text-xs">
        <p className="break-all text-sm text-white">{filename}</p>
        <div className="flex items-center justify-between gap-3"><p className="text-[#aaa]">{progress === 100 ? "Validating source files…" : `Uploading… ${progress}%`}</p><button type="button" className={buttonClass} onClick={cancelUpload}>Cancel upload</button></div>
        <progress aria-label="ZIP upload progress" value={progress} max={100} className="h-1 w-full accent-emerald-500" />
      </div> : <div className="rounded-lg border border-dashed border-[#444] p-6 text-center sm:p-8" onDragOver={event => event.preventDefault()} onDrop={event => {
        event.preventDefault();
        if (!fileInput.current?.disabled && !fileInput.current?.closest("fieldset")?.disabled) void selectFile(event.dataTransfer.files[0]);
      }}>
        <Upload className="mx-auto mb-3 h-6 w-6 text-[#888]" aria-hidden="true" />
        <p className="text-sm text-[#aaa]">Drop your ZIP codebase here</p>
        <button type="button" className={`${buttonClass} mt-3`} onClick={() => fileInput.current?.click()}>Choose ZIP</button>
        <p className="mt-3 text-xs text-[#888]">.zip · up to 100 MiB</p>
      </div>}
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
    </div>}
  </div>;
}
