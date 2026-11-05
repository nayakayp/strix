import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Bot, ChevronDown, ChevronRight } from "lucide-react";
import { AgentTranscript } from "./AgentTranscript";
import { ScanPromptComposer } from "./ScanPromptComposer";
import type { LoadedRun, TranscriptAgent, TranscriptEvent } from "@/data/serverSource";

const STATUS_DOT: Record<string, string> = {
  completed: "bg-emerald-400",
  running: "bg-blue-400",
  waiting: "bg-yellow-400",
  stopped: "bg-[#888]",
  crashed: "bg-red-400",
  failed: "bg-red-400",
};

export default function AgentBrowser({ run, canSteer }: { run: LoadedRun; canSteer: boolean }) {
  const { agents, events } = run.transcript;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const { roots, children, byId } = useMemo(() => {
    const byId = new Map(agents.map(agent => [agent.id, agent]));
    const roots: TranscriptAgent[] = [];
    const children = new Map<string, TranscriptAgent[]>();
    for (const agent of agents) {
      if (!agent.parent_id || !byId.has(agent.parent_id)) roots.push(agent);
      else children.set(agent.parent_id, [...(children.get(agent.parent_id) ?? []), agent]);
    }
    return { roots, children, byId };
  }, [agents]);
  const selected = (selectedId ? byId.get(selectedId) : null) ?? roots[0];
  const steerable = canSteer && !run.finished;

  function renderBranch(agent: TranscriptAgent) {
    const descendants = children.get(agent.id) ?? [];
    const expanded = !collapsed.has(agent.id);
    return <li key={agent.id}>
      <div className={`flex items-center rounded-lg ${selected?.id === agent.id ? "bg-emerald-500/10" : "hover:bg-white/[0.04]"}`}>
        {descendants.length > 0 ? <button
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} ${agent.name}`}
          aria-expanded={expanded}
          aria-controls={`agent-children-${agent.id}`}
          onClick={() => setCollapsed(current => {
            const next = new Set(current);
            if (next.has(agent.id)) next.delete(agent.id); else next.add(agent.id);
            return next;
          })}
          className="shrink-0 cursor-pointer rounded-md p-1.5 text-[#888] hover:text-white focus-visible:outline-emerald-400"
        >{expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</button> : <span className="w-[26px] shrink-0" />}
        <button
          type="button"
          aria-pressed={selected?.id === agent.id}
          aria-label={`${agent.name}, ${agent.status}`}
          title={agent.name}
          onClick={() => setSelectedId(agent.id)}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-lg py-3 pr-2 text-left focus-visible:outline-emerald-400"
        >
          <span className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[agent.status] ?? "bg-[#888]"}`} aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-[#ddd]">{agent.name}</span>
          <span className="shrink-0 text-[10px] capitalize text-[#888]">{agent.status}</span>
          {descendants.length > 0 && <span className="rounded border border-[#333] px-1 text-[10px] text-[#888]">{descendants.length}</span>}
        </button>
      </div>
      {descendants.length > 0 && <ul id={`agent-children-${agent.id}`} hidden={!expanded} className="ml-3 border-l border-[#292929] pl-2">{descendants.map(renderBranch)}</ul>}
    </li>;
  }

  return <div className="space-y-4">
    {agents.length === 0 ? <div className="rounded-xl border border-[#222] p-8 text-center text-sm text-[#888]">{run.finished ? "No agents recorded for this run." : "Waiting for the first agent…"}</div> : <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(16rem,0.85fr)_minmax(0,1.4fr)] lg:h-[min(56dvh,40rem)] lg:min-h-80">
      <nav aria-label="Agent hierarchy" className={`${selectedId ? "hidden lg:flex" : "flex"} min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-[#222] bg-white/[0.02]`}>
        <h2 className="flex shrink-0 items-center gap-2 border-b border-[#222] px-4 py-3 text-sm font-semibold"><Bot className="h-4 w-4 text-[#888]" aria-hidden="true" />Agents<span className="ml-auto text-xs font-normal text-[#888]">{agents.length}</span></h2>
        <ul className="min-h-0 overflow-auto p-2 scrollbar-thin">{roots.map(renderBranch)}</ul>
      </nav>
      {selected && <div className={`${selectedId ? "flex" : "hidden lg:flex"} min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-[#222] bg-white/[0.02]`}>
        <div className="shrink-0 space-y-2 border-b border-[#222] px-4 py-3">
          <button type="button" onClick={() => setSelectedId(null)} className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-[#aaa] hover:text-white lg:hidden"><ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />All agents</button>
          <div className="flex items-center gap-2"><span className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[selected.status] ?? "bg-[#888]"}`} aria-hidden="true" /><h2 className="min-w-0 break-words text-sm font-semibold">{selected.name}</h2></div>
          <p className="text-xs text-[#888]"><span className="capitalize">{selected.status}</span>{selected.parent_id && byId.has(selected.parent_id) && <> · Child of {byId.get(selected.parent_id)!.name}</>}</p>
        </div>
        <TranscriptPane key={selected.id} agent={selected} events={events} />
        {steerable && <div className="shrink-0 border-t border-[#222] p-3"><ScanPromptComposer agents={[selected]} fixedAgentId={selected.id} className="mt-0" /></div>}
      </div>}
    </div>}
    {steerable && <ScanPromptComposer agents={agents} />}
  </div>;
}

function TranscriptPane({ agent, events }: { agent: TranscriptAgent; events: TranscriptEvent[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(false);
  const [ready, setReady] = useState(false);

  // Paint the inspector shell before mounting a potentially long transcript.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!nearBottom.current) return;
    const frame = requestAnimationFrame(() => {
      const el = scrollRef.current;
      el?.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [events]);

  return <div ref={scrollRef} aria-label={`${agent.name} transcript`} role="region" tabIndex={0} onScroll={() => {
    const el = scrollRef.current!;
    nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }} className="min-h-0 flex-1 overflow-auto p-4 scrollbar-thin max-lg:max-h-[70dvh]">
    {ready && <AgentTranscript agent={agent} events={events} showHeader={false} />}
  </div>;
}
