import { useCallback, useEffect, useRef, useState } from "react";
import { Box, BrainCircuit, ChevronDown, Plug, Search, Settings2, X } from "lucide-react";
import LlmSettingsView from "@/components/LlmSettingsView";
import RunnerSettingsView from "@/components/RunnerSettingsView";
import GithubIntegrationsView from "@/components/GithubIntegrationsView";

const groups = [
  { id: "llm", label: "LLM providers", icon: BrainCircuit, entries: [
    { id: "connections", label: "Connections", keywords: "provider api key base url openai anthropic ollama" },
    { id: "models", label: "Models and pricing", keywords: "model tokens cost input output cached" },
    { id: "requests", label: "Request options", keywords: "advanced headers extra body json reasoning" },
  ] },
  { id: "runners", label: "Sandbox", icon: Box, entries: [
    { id: "e2b", label: "E2B", keywords: "sandbox runner template stable availability version release" },
  ] },
  { id: "integrations", label: "Integrations", icon: Plug, entries: [
    { id: "github", label: "GitHub", keywords: "app installation authorization connect" },
    { id: "repositories", label: "Connected repositories", keywords: "github repository repo source access" },
  ] },
];

function readSection() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const section = params.get("section");
  if (section === "defaults" || section === "daytona") return "e2b";
  if (groups.some(group => group.entries.some(entry => entry.id === section))) return section!;
  return params.get("page") === "runners" ? "e2b" : params.get("page") === "integrations" ? "github" : "connections";
}

export default function SettingsView() {
  const [selected, setSelected] = useState(readSection);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const content = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const current = groups.find(group => group.entries.some(entry => entry.id === selected))!;
  const entry = current.entries.find(item => item.id === selected)!;
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const matches = groups.map(group => ({ ...group, entries: group.entries.filter(item => terms.every(term => `${group.label} ${item.label} ${item.keywords}`.toLowerCase().includes(term))) })).filter(group => group.entries.length > 0);
  const count = matches.reduce((total, group) => total + group.entries.length, 0);

  useEffect(() => {
    const navigate = () => setSelected(readSection());
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);

  const reveal = useCallback(() => {
    const panel = content.current;
    if (!panel) return false;
    const target = selected === current.entries[0].id ? panel : document.getElementById(`settings-${selected}`);
    if (!target) return false;
    if (target instanceof HTMLDetailsElement) target.open = true;
    if (window.matchMedia("(min-width: 1024px)").matches) {
      panel.scrollTop = target === panel ? 0 : target.getBoundingClientRect().top - panel.getBoundingClientRect().top + panel.scrollTop - 16;
    } else target.scrollIntoView({ block: "start" });
    return true;
  }, [selected, current]);

  useEffect(() => {
    const panel = content.current;
    if (!panel) return;
    if (reveal()) return;
    // Runner sections arrive after their settings request completes.
    const observer = new MutationObserver(() => { if (reveal()) observer.disconnect(); });
    observer.observe(panel, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [reveal]);

  return <div className="grid min-w-0 gap-6 lg:grid-cols-[220px_minmax(0,1fr)] lg:gap-8">
    <aside className="min-w-0 space-y-5 lg:sticky lg:top-6 lg:self-start">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><Settings2 className="h-5 w-5 text-[#888]" aria-hidden="true" />Settings</h1>
        <p className="mt-2 text-xs leading-relaxed text-[#888]">One place for your configuration. Changes are saved per section.</p>
      </div>
      <div>
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-[#888]" aria-hidden="true" />
          <input ref={search} type="search" aria-label="Search settings" placeholder="Search settings…" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === "Escape") setQuery(""); }} className="w-full appearance-none rounded-lg border border-[#333] bg-black py-2.5 pl-9 pr-9 text-sm focus:border-emerald-500 focus:outline-none [&::-webkit-search-cancel-button]:appearance-none" />
          {query && <button aria-label="Clear settings search" onClick={() => { setQuery(""); search.current?.focus(); }} className="absolute right-2 top-2 cursor-pointer rounded p-1 text-[#888] hover:text-white"><X className="h-4 w-4" aria-hidden="true" /></button>}
        </div>
        <p className="mt-2 text-[11px] text-[#888]">Try headers, model, template, GitHub</p>
        {terms.length > 0 && <p role="status" className="mt-3 text-xs text-[#aaa]">{count} matching {count === 1 ? "setting" : "settings"}</p>}
      </div>
      <button className="flex w-full cursor-pointer items-center justify-between rounded-lg border border-[#333] p-3 text-sm lg:hidden" aria-expanded={mobileOpen || terms.length > 0} aria-controls="settings-navigation" onClick={() => setMobileOpen(!mobileOpen)}>{current.label}<ChevronDown className={`h-4 w-4 transition-transform ${mobileOpen ? "rotate-180" : ""}`} aria-hidden="true" /></button>
      <nav id="settings-navigation" aria-label="Settings navigation" className={`${mobileOpen || terms.length ? "block" : "hidden"} max-h-72 space-y-3 overflow-y-auto scrollbar-thin lg:block lg:max-h-[calc(100dvh-20rem)]`}>
        {matches.map(group => {
          const open = terms.length > 0 || !collapsed.includes(group.id);
          const Icon = group.icon;
          return <div key={group.id} className="border-t border-[#222] pt-3">
            <button className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-left text-sm font-medium text-[#ddd] hover:bg-white/[0.05] focus-visible:outline-emerald-500" aria-expanded={open} aria-controls={`settings-nav-${group.id}`} onClick={() => { if (!terms.length) setCollapsed(value => value.includes(group.id) ? value.filter(id => id !== group.id) : [...value, group.id]); }}>
              <Icon className="h-4 w-4 text-[#888]" aria-hidden="true" /><span className="flex-1">{group.label}</span><ChevronDown className={`h-3.5 w-3.5 text-[#888] transition-transform ${open ? "" : "-rotate-90"}`} aria-hidden="true" />
            </button>
            <div id={`settings-nav-${group.id}`} hidden={!open} className="ml-4 mt-1 space-y-1 border-l border-[#292929] pl-2">
              {group.entries.map(item => <a key={item.id} href={`#page=settings&section=${item.id}`} aria-current={selected === item.id ? "location" : undefined} onClick={() => { setMobileOpen(false); if (selected === item.id) requestAnimationFrame(reveal); }} className={`block rounded-md border-l-2 px-3 py-2 text-[13px] transition-colors focus-visible:outline-emerald-500 ${selected === item.id ? "border-emerald-400 bg-emerald-500/10 text-emerald-300" : "border-transparent text-[#999] hover:bg-white/[0.05] hover:text-white"}`}>{item.label}</a>)}
            </div>
          </div>;
        })}
        {!count && <p className="rounded-lg border border-[#222] p-3 text-xs leading-relaxed text-[#999]">No settings match “{query}”. Try a different term or clear the search.</p>}
      </nav>
      <p className="hidden text-[11px] leading-relaxed text-[#666] lg:block">Provider and runner drafts stay here while you switch sections.</p>
    </aside>
    <div className="min-w-0">
      <p className="mb-5 flex flex-wrap gap-2 text-xs text-[#888]" aria-label="Settings location"><span>Settings</span><span aria-hidden="true">/</span><span>{current.label}</span><span aria-hidden="true">/</span><span className="text-[#ddd]">{entry.label}</span></p>
      <div ref={content} className="min-w-0 pr-1 scrollbar-thin lg:max-h-[calc(100dvh-15rem)] lg:overflow-y-auto lg:pr-4">
        {/* Keep each form mounted so section navigation never discards its draft. */}
        <div hidden={current.id !== "llm"}><LlmSettingsView /></div>
        <div hidden={current.id !== "runners"}><RunnerSettingsView /></div>
        <div hidden={current.id !== "integrations"}><GithubIntegrationsView /></div>
      </div>
    </div>
  </div>;
}
