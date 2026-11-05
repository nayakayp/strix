import { useEffect, useState } from "react";
import { Box, RefreshCw } from "lucide-react";
import { checkRunner, fetchRunnerSettings, type RunnerCheck, type RunnerSettings } from "@/data/serverSource";

const button = "inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export function RunnerCheckStatus({ check }: { check: RunnerCheck | null }) {
  if (!check || (check.availability === "available" && check.compatibility !== "incompatible")) return null;
  return <div role="alert" className="space-y-1 rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-300">
    <p className="font-medium">{check.availability === "available" ? "Available · incompatible version" : { missing: "Image not found", unavailable: "Image not ready", unconfigured: "API key not configured", error: "Availability check failed" }[check.availability]}</p>
    <p className="leading-relaxed">{check.message}</p>
  </div>;
}

export default function RunnerSettingsView() {
  const [settings, setSettings] = useState<RunnerSettings | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRunnerSettings().then(data => { if (!cancelled) setSettings(data); }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  async function check() {
    setPending(true); setError(null);
    try {
      const result = await checkRunner("e2b");
      setSettings(current => current && { ...current, images: current.images.map(image => ({ ...image, check: result })) });
    } catch (e) { setError(e instanceof Error ? e.message : "Could not check image."); }
    finally { setPending(false); }
  }

  return <div className="max-w-4xl space-y-6">
    <div>
      <h2 className="flex items-center gap-2 text-2xl font-semibold"><Box className="h-5 w-5 text-[#888]" aria-hidden="true" />E2B sandbox</h2>
      <p className="mt-1 text-sm text-[#888]">All dashboard scans run in isolated E2B sandboxes.</p>
    </div>
    <div className="rounded-xl border border-[#222] bg-white/[0.02] p-4 text-xs leading-relaxed text-[#aaa]">
      <p>Set <code>E2B_API_KEY</code> in the server environment. Checking availability never creates a sandbox or starts a scan.</p>
    </div>
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    {!settings ? <p className="text-sm text-[#888]">{error ? "Settings unavailable. Reload to try again." : "Loading runner settings…"}</p> : <div className="space-y-6">
        {settings.images.map(image => <section key={image.id} id={`settings-${image.id}`} aria-label={`${image.name} runner`} className="space-y-4 rounded-xl border border-[#222] bg-white/[0.02] p-5">
            <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">{image.name}</h2><span className={`text-xs ${image.configured ? "text-emerald-400" : "text-red-300"}`}>{image.configured ? "API key configured" : "API key not configured"}</span></div>
            <p className="text-xs text-[#aaa]">Template source<span className="mt-1 block break-all font-mono text-white">{image.template}</span></p>
            <p className="text-xs leading-relaxed text-[#888]">New scans use the published stable release, independent of local dashboard changes. Each scan pins its release; existing scans stay unchanged. Template overrides are not used.</p>
            <RunnerCheckStatus check={image.check} />
            <button type="button" className={button} disabled={pending || !image.configured} onClick={() => void check()}><RefreshCw className={`h-3.5 w-3.5 ${pending ? "animate-spin" : ""}`} aria-hidden="true" />{pending ? "Checking…" : `Check ${image.name} availability`}</button>
          </section>)}
    </div>}
  </div>;
}
