import { useEffect, useState, type FormEvent } from "react";
import { Plus, RefreshCw, Save, Settings2 } from "lucide-react";
import { fetchLlmSettings, fetchProviderModels, updateLlmSettings, type LlmProvider, type LlmSettings, type ModelPrices, type ProviderModel } from "@/data/serverSource";

const TYPES = {
  openai: { label: "OpenAI", url: "https://api.openai.com/v1" },
  openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api/v1" },
  anthropic: { label: "Anthropic", url: "https://api.anthropic.com" },
  ollama: { label: "Ollama", url: "http://localhost:11434" },
  custom: { label: "OpenAI-compatible", url: "" },
};
type Draft = Omit<LlmProvider, "id" | "has_api_key"> & { id?: string; api_key: string };
const empty = (): Draft => ({ name: "", kind: "openai", base_url: TYPES.openai.url, model: "", api_key: "", prices: {} });
const input = "mt-1 w-full rounded-lg border border-[#333] bg-black px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none disabled:opacity-50";
const button = "inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#333] px-3 py-2 text-xs text-[#ccc] transition-colors hover:border-[#666] hover:text-white disabled:cursor-not-allowed disabled:opacity-40";

export default function LlmSettingsView() {
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [draft, setDraft] = useState<Draft>(empty);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [clearKey, setClearKey] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const saved = settings?.providers.find(p => p.id === draft.id);
  const active = settings?.providers.find(p => p.id === settings.selected_id);
  const prices = draft.prices[draft.model] ?? {};
  const fetchedPrice = models.find(m => m.id === draft.model)?.prices;

  useEffect(() => {
    let cancelled = false;
    fetchLlmSettings().then(data => { if (!cancelled) setSettings(data); }).catch(e => { if (!cancelled) setError(String(e.message)); });
    return () => { cancelled = true; };
  }, []);

  function edit(provider?: LlmProvider) {
    if (provider) {
      const { has_api_key: _, ...fields } = provider;
      setDraft({ ...fields, api_key: "" });
    } else setDraft(empty());
    setClearKey(false);
    setModels([]);
    setError(null);
    setNotice(null);
  }

  async function perform(action: () => Promise<void>) {
    setPending(true);
    setError(null);
    setNotice(null);
    try { await action(); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not update settings."); }
    finally { setPending(false); }
  }

  async function save(): Promise<LlmProvider> {
    const data = await updateLlmSettings("providers", { ...draft, clear_api_key: clearKey });
    const provider = data.providers[data.providers.length - 1];
    setSettings(data);
    const { has_api_key: _, ...fields } = provider;
    setDraft({ ...fields, api_key: "" });
    setClearKey(false);
    return provider;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void perform(async () => { await save(); setNotice("Provider saved. Select Use for new scans to make it the default."); });
  }

  function price(name: keyof ModelPrices, value: string) {
    setDraft({ ...draft, prices: { ...draft.prices, [draft.model]: { ...prices, [name]: value === "" ? null : Number(value) } } });
  }

  return <div className="space-y-6">
    <div>
      <h1 className="flex items-center gap-2 text-2xl font-semibold"><Settings2 className="h-5 w-5 text-[#888]" aria-hidden="true" />LLM settings</h1>
      <p className="mt-1 text-sm text-[#888]">Provider connections, models, and estimated token prices for new local scans.</p>
    </div>
    <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-sm">
      <p className="text-emerald-400">{active ? `Default: ${active.name} · ${active.model}` : "Default: server CLI settings"}</p>
      <p className="mt-1 text-xs text-[#888]">Changes apply to new dashboard scans only. Running scans and CLI defaults are unchanged.</p>
    </div>
    {error && <p role="alert" className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-sm text-red-300">{error}</p>}
    {notice && <p role="status" className="text-sm text-emerald-400">{notice}</p>}
    <section aria-label="Providers" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Providers</h2>
        <button disabled={pending} className={button} onClick={() => edit()}><Plus className="h-3.5 w-3.5" aria-hidden="true" />Add provider</button>
      </div>
      {settings === null ? <p className="text-sm text-[#888]">{error ? "Settings unavailable. Reload to try again." : "Loading settings…"}</p> : settings.providers.length === 0 ? <p className="text-sm text-[#888]">No saved providers. Add your first connection below.</p> : <div className="grid gap-3 md:grid-cols-2">{settings.providers.map(p => <button disabled={pending} key={p.id} onClick={() => edit(p)} className={`cursor-pointer rounded-xl border bg-white/[0.02] p-4 text-left transition-colors hover:border-[#666] ${draft.id === p.id ? "border-emerald-500/50" : "border-[#222]"}`}>
        <div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{p.name}</span>{settings.selected_id === p.id && <span className="rounded border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-400">Default for new scans</span>}</div>
        <p className="mt-1 break-all text-xs text-[#888]">{TYPES[p.kind].label} · {p.model || "No model selected"}</p>
        <p className="mt-1 break-all text-xs text-[#666]">{p.base_url}</p>
      </button>)}</div>}
      {active && <button disabled={pending} className={button} onClick={() => void perform(async () => { setSettings(await updateLlmSettings("select", { id: null })); setNotice("New scans will use server CLI settings."); })}>Use server CLI settings instead</button>}
    </section>
    <form onSubmit={submit} className="rounded-xl border border-[#222] bg-white/[0.02] p-5">
      <fieldset disabled={pending || settings === null} className="space-y-5">
        <h2 className="text-sm font-semibold">{draft.id ? "Edit provider" : "Add provider connection"}</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm text-[#aaa]">Provider type
            <select className={input} value={draft.kind} onChange={e => { const kind = e.target.value as Draft["kind"]; setDraft({ ...draft, kind, base_url: TYPES[kind].url, model: "", api_key: "", prices: {} }); setClearKey(true); setModels([]); }}>
              {Object.entries(TYPES).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
            </select>
          </label>
          <label className="block text-sm text-[#aaa]">Connection name<input required maxLength={100} className={input} value={draft.name} placeholder="My provider" onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        </div>
        <label className="block text-sm text-[#aaa]">Base URL<input required type="url" maxLength={2048} className={input} value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })} />
          <span className="mt-1 block text-xs text-[#888]">Reached from this server, not your browser. Include /v1 for OpenAI-compatible APIs; use the server root for Anthropic and Ollama.</span>
        </label>
        {draft.base_url.startsWith("http:") && <p className="text-xs text-amber-400">HTTP is unencrypted. Use only on a trusted local network; use HTTPS for remote providers.</p>}
        <label className="block text-sm text-[#aaa]">API key<input type="password" autoComplete="new-password" maxLength={4096} className={input} value={draft.api_key} placeholder={saved?.has_api_key ? "Key saved — leave blank to keep it" : "Enter API key (optional for local providers)"} onChange={e => setDraft({ ...draft, api_key: e.target.value })} />
          <span className="mt-1 block text-xs text-[#888]">Write-only. Stored per workspace under ~/.strix/dashboard with owner-only permissions, not encrypted. Excluded from scan sources. Never returned to the browser.</span>
        </label>
        {saved?.has_api_key && <label className="flex items-center gap-2 text-xs text-[#aaa]"><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)} className="accent-emerald-500" />Remove saved API key on save</label>}
        <div className="border-t border-[#222] pt-4 space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="mr-auto text-sm font-semibold">Model selection</h3>
            <button type="button" className={button} onClick={e => { if (!e.currentTarget.form?.reportValidity()) return; void perform(async () => { const provider = await save(); const list = await fetchProviderModels(provider.id); setModels(list); setNotice(`${list.length} models fetched. Choose a model or enter its ID manually.`); }); }}><RefreshCw className={`h-3.5 w-3.5 ${pending ? "animate-spin" : ""}`} aria-hidden="true" />{pending ? "Working…" : "Save & fetch models"}</button>
          </div>
          <label className="block text-sm text-[#aaa]">Model ID<input list="provider-models" autoComplete="off" maxLength={300} className={input} value={draft.model} placeholder="Choose a fetched model or enter its ID" onChange={e => setDraft({ ...draft, model: e.target.value })} />
            <datalist id="provider-models">{models.map(m => <option key={m.id} value={m.id} />)}</datalist>
            <span className="mt-1 block text-xs text-[#888]">Use the provider’s model ID, without a Strix routing prefix. Fetching models does not run a scan or generate tokens.</span>
          </label>
        </div>
        <div className="border-t border-[#222] pt-4 space-y-3">
          <h3 className="text-sm font-semibold">Estimated pricing <span className="text-xs font-normal text-[#888]">USD per 1 million tokens</span></h3>
          <p className="text-xs leading-relaxed text-[#888]">Leave all prices blank for automatic accounting. For overrides, set both input and output; zero means free. Blank cached input uses the input price. Prices are saved separately for each model and do not change provider billing.</p>
          <div className="grid gap-4 sm:grid-cols-3">{([["input", "Input price"], ["output", "Output price"], ["cached_input", "Cached input price"]] as const).map(([key, label]) => <label key={key} className="block text-sm text-[#aaa]">{label}<input disabled={!draft.model || pending} required={key !== "cached_input" && (prices.input != null || prices.output != null || prices.cached_input != null)} type="number" min="0" step="any" value={prices[key] ?? ""} placeholder="Automatic" className={input} onChange={e => price(key, e.target.value)} /></label>)}</div>
          {fetchedPrice?.input != null && fetchedPrice.output != null && <button type="button" className={button} onClick={() => setDraft({ ...draft, prices: { ...draft.prices, [draft.model]: fetchedPrice } })}>Use fetched prices (${fetchedPrice.input} input / ${fetchedPrice.output} output per 1M)</button>}
        </div>
        <div className="flex flex-wrap gap-2 border-t border-[#222] pt-4">
          <button type="submit" className={`${button} bg-white !text-black hover:bg-[#ddd]`}><Save className="h-3.5 w-3.5" aria-hidden="true" />Save provider</button>
          <button type="button" disabled={!draft.model} className={`${button} !border-emerald-500/40 !text-emerald-400`} onClick={e => { if (!e.currentTarget.form?.reportValidity()) return; void perform(async () => { const provider = await save(); setSettings(await updateLlmSettings("select", { id: provider.id })); setNotice("Default saved. New local scans will use this provider and model."); }); }}>Save & use for new scans</button>
          {draft.id && <button type="button" className={`${button} sm:ml-auto !text-red-400`} onClick={() => { if (!window.confirm(`Delete ${draft.name}? Running scans are unaffected.`)) return; void perform(async () => { const data = await updateLlmSettings("delete", { id: draft.id }); setSettings(data); edit(); setNotice("Provider deleted."); }); }}>Delete provider</button>}
        </div>
      </fieldset>
    </form>
  </div>;
}
