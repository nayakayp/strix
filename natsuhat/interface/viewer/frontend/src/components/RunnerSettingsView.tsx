import { useEffect, useState, type FormEvent } from "react";
import { Box, RefreshCw, Save } from "lucide-react";
import { checkRunner, fetchRunnerSettings, saveRunnerSettings, type RunnerCheck, type RunnerSettings } from "@/data/serverSource";

const input = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const button = "inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export function RunnerCheckStatus({ check }: { check: RunnerCheck | null }) {
  if (!check) return <p className="text-xs text-[#888]">Not checked. Availability checks read provider metadata only; they do not create a sandbox.</p>;
  const blocked = check.availability !== "available" || check.compatibility === "incompatible";
  return <div role="status" className={`space-y-1 rounded-lg border p-3 text-xs ${blocked ? "border-red-500/30 bg-red-500/5 text-red-300" : "border-amber-500/30 bg-amber-500/5 text-amber-300"}`}>
    <p className="font-medium">{check.availability === "available" ? check.compatibility === "incompatible" ? "Available · incompatible version" : check.compatibility === "revision_matches" ? "Available · release matches" : "Available · contents unverified" : { missing: "Image not found", unavailable: "Image not ready", unconfigured: "API key not configured", error: "Availability check failed" }[check.availability]}</p>
    <p className="leading-relaxed">{check.message}</p>
    {check.release && <p className="break-all text-[#aaa]">Resolved release: {check.release}</p>}
    {check.template_id && <p className="break-all text-[#aaa]">Provider ID: {check.template_id}</p>}
    {check.build_id && <p className="break-all text-[#aaa]">Build ID: {check.build_id}</p>}
    <p className="text-[#888]">Checked {new Date(check.checked_at).toLocaleString()} · expires after 60 seconds</p>
  </div>;
}

export default function RunnerSettingsView() {
  const [settings, setSettings] = useState<RunnerSettings | null>(null);
  const [draft, setDraft] = useState<Pick<RunnerSettings, "default_runner" | "e2b_template" | "daytona_snapshot">>({ default_runner: null, e2b_template: "", daytona_snapshot: "" });
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const dirty = settings !== null && (draft.default_runner !== settings.default_runner || draft.e2b_template !== settings.e2b_template || draft.daytona_snapshot !== settings.daytona_snapshot);

  useEffect(() => {
    let cancelled = false;
    fetchRunnerSettings().then(data => { if (!cancelled) { setSettings(data); setDraft(data); } }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setPending("save"); setError(null); setNotice(null);
    try {
      const data = await saveRunnerSettings({ default_runner: draft.default_runner, e2b_template: draft.e2b_template.trim(), daytona_snapshot: draft.daytona_snapshot.trim() });
      setSettings(data); setDraft(data);
      setNotice("Runner settings saved for new dashboard scans. Existing jobs are unchanged.");
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save settings."); }
    finally { setPending(null); }
  }

  async function check(provider: "e2b" | "daytona") {
    setPending(provider); setError(null); setNotice(null);
    try {
      const result = await checkRunner(provider);
      setSettings(current => current && { ...current, images: current.images.map(image => image.id === provider ? { ...image, check: result } : image) });
    } catch (e) { setError(e instanceof Error ? e.message : "Could not check image."); }
    finally { setPending(null); }
  }

  return <div className="max-w-4xl space-y-6">
    <div>
      <h1 className="flex items-center gap-2 text-2xl font-semibold"><Box className="h-5 w-5 text-[#888]" aria-hidden="true" />Sandbox runners</h1>
      <p className="mt-1 text-sm text-[#888]">Choose where new scans run and which prepared template they use.</p>
    </div>
    <div className="rounded-xl border border-[#222] bg-white/[0.02] p-4 text-xs leading-relaxed text-[#aaa]">
      <p>Templates are saved in your E2B or Daytona account. This workspace saves their selection under <code>~/.natsuhat/dashboard</code>, outside scan sources.</p>
      <p className="mt-2">Saving or checking settings never builds an image, starts a scan, or creates a billable sandbox. Provider API keys remain server environment settings and are never returned here.</p>
    </div>
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    {notice && <p role="status" className="text-sm text-emerald-400">{notice}</p>}
    {!settings ? <p className="text-sm text-[#888]">{error ? "Settings unavailable. Reload to try again." : "Loading runner settings…"}</p> : <form onSubmit={save}>
      <fieldset disabled={pending !== null} className="space-y-6">
        <label className="block text-sm text-[#aaa]">Default runner
          <select className={input} value={draft.default_runner ?? ""} onChange={e => setDraft({ ...draft, default_runner: (e.target.value || null) as RunnerSettings["default_runner"] })}>
            <option value="">Automatic</option>
            <option value="local">Local Docker</option><option value="e2b">E2B</option><option value="daytona">Daytona</option>
          </select>
          <span className="mt-2 block text-xs text-[#888]">Current default: {settings.effective_default_runner === "local" ? "Local Docker" : settings.effective_default_runner === "e2b" ? "E2B" : "Daytona"}. Automatic uses E2B when its API key is set, otherwise Local Docker. Local scans require Docker and the local sandbox image; remote image checks do not verify local Docker.</span>
        </label>
        <section aria-label="Required runner version" className="space-y-2 rounded-xl border border-[#222] p-4">
          <h2 className="text-sm font-semibold">Required by this application</h2>
          <p className="break-all font-mono text-sm text-emerald-400">{settings.required_template}</p>
          <p className="break-all text-xs text-[#888]">Full content revision: {settings.required_revision}</p>
          <p className="text-xs leading-relaxed text-[#aaa]">Derived from application and build files, not a Git commit. Source changes can require a new template. Selecting an older image does not update its application code.</p>
        </section>
        {settings.images.map(image => {
          const field = image.id === "e2b" ? "e2b_template" : "daytona_snapshot";
          return <section key={image.id} aria-label={`${image.name} runner`} className="space-y-4 rounded-xl border border-[#222] bg-white/[0.02] p-5">
            <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">{image.name}</h2><span className={`text-xs ${image.configured ? "text-emerald-400" : "text-amber-400"}`}>{image.configured ? "API key configured" : "API key not configured"}</span></div>
            <label className="block text-sm text-[#aaa]">{image.id === "e2b" ? "E2B template name, tag, or ID" : "Daytona snapshot name or ID"}
              <input className={input} maxLength={256} value={draft[field]} placeholder="Automatic — use required version" onChange={e => setDraft({ ...draft, [field]: e.target.value })} />
              <span className="mt-2 block text-xs text-[#888]">Leave blank to use the server environment override, or the required version when no override is set.</span>
              {image.id === "e2b" && <span className="mt-2 block text-xs leading-relaxed text-[#888]">Use natsuhat-runner:stable for the published release. New scans pin its content-revision tag; updating stable does not change existing jobs. A new application revision requires republishing the template.</span>}
            </label>
            <p className="text-xs text-[#aaa]">Effective template <span className="text-[#666]">({image.selection_source})</span><span className="mt-1 block break-all font-mono text-white">{image.template}</span></p>
            <RunnerCheckStatus check={image.check} />
            <button type="button" className={button} disabled={dirty || !image.configured} onClick={() => void check(image.id)}><RefreshCw className={`h-3.5 w-3.5 ${pending === image.id ? "animate-spin" : ""}`} aria-hidden="true" />{pending === image.id ? "Checking…" : `Check ${image.name} availability`}</button>
            <p className="text-xs text-[#888]">{dirty ? "Save changes before checking the selected image." : "Reads provider metadata only. Matching names are not proof of image contents; the worker verifies the full manifest before sending model credentials."}</p>
          </section>;
        })}
        <button type="submit" className={`${button} bg-white !text-black hover:bg-[#ddd]`}><Save className="h-3.5 w-3.5" aria-hidden="true" />{pending === "save" ? "Saving…" : "Save runner settings"}</button>
      </fieldset>
    </form>}
  </div>;
}
