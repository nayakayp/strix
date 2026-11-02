import { useEffect, useState } from "react";
import { Github, RefreshCw } from "lucide-react";
import { fetchGithub, updateGithub, type GithubState } from "@/data/serverSource";

const buttonClass = "inline-flex cursor-pointer items-center gap-2 rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export default function GithubIntegrationsView() {
  const [state, setState] = useState<GithubState | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchGithub().then(value => { if (!cancelled) setState(value); }).catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  async function action(name: string, id?: number) {
    setPending(true); setError(null); setMessage(null);
    try {
      const result = name === "refresh" ? await fetchGithub(true) : await updateGithub(name, id);
      if ("url" in result && typeof result.url === "string") { window.location.assign(result.url); return; }
      setState(result);
      setMessage(name === "add" ? "Repository connected. Create a scan from Pentest → New scan." : name === "remove" ? "Repository disconnected. Existing scan results are unchanged." : name === "disconnect" ? "GitHub disconnected locally. Uninstall the App on GitHub to revoke its access." : "Repository access refreshed. Reconnect GitHub to authorize newly granted repositories.");
    } catch (e) { setError(e instanceof Error ? e.message : "Could not update GitHub."); }
    finally { setPending(false); }
  }

  return <div className="max-w-4xl space-y-6">
    <div><h1 className="text-2xl font-semibold">Integrations</h1><p className="mt-1 text-sm text-[#888]">Connect source repositories for authorized codebase scans.</p></div>
    <section aria-label="GitHub integration" className="space-y-5 rounded-xl border border-[#222] bg-white/[0.02] p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold"><Github className="h-5 w-5" aria-hidden="true" />GitHub</h2>
        <span className={`text-xs ${state?.connected ? "text-emerald-400" : "text-[#888]"}`}>{state ? state.connected ? "Connected" : state.configured ? "Ready to connect" : "Server setup required" : "Loading…"}</span>
      </div>
      <p className="text-sm leading-relaxed text-[#aaa]">Read-only access to repositories selected in your GitHub App installation. GitHub credentials stay on this server; scans receive only source snapshots. Connecting never starts a scan.</p>
      {state && !state.configured && <div className="space-y-3 rounded-lg border border-[#333] bg-black p-4 text-xs leading-relaxed text-[#aaa]">
        <p className="font-semibold text-white">Configure a self-hosted GitHub App</p>
        <ol className="list-decimal space-y-2 pl-4">
          <li>Register a GitHub App with Contents: read-only and Metadata: read-only. Disable webhooks and automatic OAuth authorization during installation.</li>
          <li>Set both the Setup URL and user authorization Callback URL to your dashboard’s public HTTPS origin followed by <code>/api/github/callback</code>. Enable “Redirect on update”.</li>
          <li>Set these server environment variables through your secret manager, then restart the dashboard:</li>
        </ol>
        <pre className="overflow-x-auto text-[11px] text-emerald-400">{["NATSUHAT_GITHUB_APP_ID", "NATSUHAT_GITHUB_PRIVATE_KEY_PATH", "NATSUHAT_GITHUB_CLIENT_ID", "NATSUHAT_GITHUB_CLIENT_SECRET", "NATSUHAT_GITHUB_CALLBACK_URL"].join("\n")}</pre>
        <p>Keep the private key outside repositories and uploads. Never paste credentials into scan instructions. Use a stable callback origin; temporary portal URLs must be updated if they change.</p>
      </div>}
      {state && <div className="flex flex-wrap gap-2">
        <button className={`${buttonClass} bg-white !text-black`} disabled={pending || !state.configured} onClick={() => void action("connect")}>{state.connected ? "Reconnect / manage access" : "Connect GitHub"}</button>
        {state.connected && <>
          <button className={buttonClass} disabled={pending || !state.configured} onClick={() => void action("refresh")}><RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />Refresh repositories</button>
          <button className={`${buttonClass} !text-red-400`} disabled={pending} onClick={() => { if (window.confirm("Disconnect GitHub? New GitHub scans will be blocked. Existing results and retained source snapshots stay unchanged.")) void action("disconnect"); }}>Disconnect GitHub</button>
        </>}
      </div>}
      {pending && <p role="status" className="text-xs text-[#888]">Contacting GitHub…</p>}
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      {message && <p role="status" className="text-xs text-emerald-400">{message}</p>}
    </section>
    {state?.connected && <section aria-label="Connected repositories" className="space-y-3">
      <h2 className="text-sm font-semibold">Connected repositories</h2>
      {state.repositories.length === 0 && <p className="text-sm text-[#888]">No repositories connected yet. Refresh repositories, then connect the ones you want to scan.</p>}
      {state.repositories.map(repository => <div key={repository.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#222] p-4">
        <div className="min-w-0"><p className="break-all text-sm">{repository.full_name}</p><p className="mt-1 break-all text-xs text-[#888]">{repository.private ? "Private" : "Public"} · Default branch: {repository.default_branch}</p></div>
        <button className={buttonClass} disabled={pending} onClick={() => void action("remove", repository.id)}>Disconnect repository</button>
      </div>)}
      {state.available.filter(r => !state.repositories.some(c => c.id === r.id)).map(repository => <div key={repository.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#222] p-4">
        <p className="break-all text-sm text-[#aaa]">{repository.full_name} <span className="text-xs text-[#666]">{repository.private ? "Private" : "Public"}</span></p>
        <button className={`${buttonClass} !text-emerald-400`} disabled={pending} onClick={() => void action("add", repository.id)}>Connect repository</button>
      </div>)}
    </section>}
  </div>;
}
