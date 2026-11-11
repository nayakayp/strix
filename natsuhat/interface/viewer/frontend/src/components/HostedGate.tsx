import { createContext, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronUp, LogOut, Users } from "lucide-react";
import { setHostedWorkspace, workspaceHeaders } from "@/data/serverSource";
import Sidebar from "@/components/Sidebar";

type Workspace = { id: string; name: string; role: string };
export type HostedSession = {
  hosted: boolean;
  user?: { id: string; email: string } | null;
  workspace?: string;
  role?: string;
  aal?: string;
  workspaces?: Workspace[];
};
export const HostedContext = createContext<(HostedSession & { accountMenu: ReactNode }) | null>(null);

async function auth(action: string, body?: Record<string, unknown>) {
  const response = await fetch(`/api/auth/${action}`, body === undefined ? { cache: "no-store", headers: workspaceHeaders() } : {
    method: "POST", headers: { "Content-Type": "application/json", ...workspaceHeaders() }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? "Account request failed.");
  return data;
}

const input = "mt-2 w-full rounded-lg border border-[#333] bg-black px-3 py-3 text-sm text-white focus:border-emerald-500 focus:outline-none";
const button = "cursor-pointer rounded-lg border border-[#444] px-4 py-2.5 text-sm text-white hover:border-[#888] focus-visible:outline-emerald-400 disabled:cursor-wait disabled:opacity-50";

export default function HostedGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<HostedSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [settingPassword, setSettingPassword] = useState(false);
  const [account, setAccount] = useState(false);
  const confirmation = useRef(new URLSearchParams(window.location.hash.slice(1)));
  const [confirmLink, setConfirmLink] = useState(() => confirmation.current.has("token_hash"));
  const refresh = useCallback(async () => {
    const value = await auth("session") as HostedSession;
    setHostedWorkspace(value.hosted ? value.workspace : undefined);
    setSession(previous => previous?.workspace !== value.workspace || previous?.user?.id !== value.user?.id
      ? value : { ...previous, ...value });
  }, []);

  useEffect(() => {
    if (confirmation.current.has("token_hash")) {
      // Link credentials stay in memory, never in browser storage or subsequent URLs.
      window.history.replaceState(null, "", window.location.pathname);
    }
    void refresh().catch(e => setError(e.message));
    const timer = setInterval(() => void refresh().catch(() => {}), 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const closeAccount = () => setAccount(false);
    window.addEventListener("hashchange", closeAccount);
    return () => window.removeEventListener("hashchange", closeAccount);
  }, []);

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(null); setNotice(null);
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : "Account request failed."); }
    finally { setBusy(false); }
  }

  if (session && !session.hosted) return <>{children}</>;
  if (!session) return <main className="min-h-screen bg-black p-8 text-sm text-[#aaa]" role="status">{error ?? "Loading dashboard…"}{error && <button className={`${button} ml-4`} onClick={() => void refresh().then(() => setError(null)).catch(e => setError(e.message))}>Retry</button>}</main>;

  if (!session.user || confirmLink || settingPassword) return <main className="flex min-h-screen items-center justify-center bg-black px-5 py-12 text-white">
    <section className="w-full max-w-md space-y-7 rounded-2xl border border-[#222] p-6 sm:p-8">
      <div className="flex items-center gap-3"><img src="./logo.png" alt="" className="h-9 w-9" /><span className="text-sm font-medium">Natsuhat</span></div>
      <div><h1 className="text-2xl font-semibold">{confirmLink ? "Accept your secure link" : settingPassword ? "Set your password" : recovering ? "Reset your password" : "Sign in to your workspace"}</h1><p className="mt-3 text-sm leading-relaxed text-[#999]">{confirmLink ? "Continue to accept your invitation or recover your account." : settingPassword ? "Choose a password with at least 12 characters." : recovering ? "We’ll send a recovery link if your account exists." : "Private scans, findings, and reports. Access is invitation-only."}</p></div>
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
      {notice && <p role="status" className="text-sm text-emerald-300">{notice}</p>}
      {confirmLink ? <button disabled={busy} className={`${button} w-full`} onClick={() => void perform(async () => {
        await auth("verify", { token_hash: confirmation.current.get("token_hash"), type: confirmation.current.get("type") });
        const data = await auth("factors");
        confirmation.current = new URLSearchParams(); setConfirmLink(false);
        if (data.factors?.some((item: Factor) => item.status === "verified")) setAccount(true);
        else setSettingPassword(true);
        await refresh();
      })}>{busy ? "Verifying…" : "Continue"}</button> : <form className="space-y-5" onSubmit={event => { event.preventDefault(); void perform(async () => {
        if (settingPassword) { await auth("password", { password }); setPassword(""); setSettingPassword(false); await refresh(); }
        else if (recovering) { const result = await auth("recover", { email }); setNotice(result.message); }
        else { await auth("login", { email, password }); setPassword(""); await refresh(); }
      }); }}>
        {!settingPassword && <label className="block text-sm text-[#ccc]">Email<input className={input} type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /></label>}
        {!recovering && <label className="block text-sm text-[#ccc]">Password<input className={input} type="password" autoComplete={settingPassword ? "new-password" : "current-password"} minLength={settingPassword ? 12 : undefined} required value={password} onChange={e => setPassword(e.target.value)} /></label>}
        <button disabled={busy} className="w-full cursor-pointer rounded-lg bg-white px-4 py-3 text-sm font-medium text-black hover:bg-neutral-200 disabled:opacity-50">{busy ? "Please wait…" : settingPassword ? "Save password" : recovering ? "Send recovery link" : "Sign in"}</button>
      </form>}
      {!confirmLink && !settingPassword && <button className="cursor-pointer text-sm text-[#aaa] underline underline-offset-4 hover:text-white" onClick={() => { setRecovering(!recovering); setError(null); setNotice(null); }}>{recovering ? "Back to sign in" : "Forgot password?"}</button>}
      <p className="border-t border-[#222] pt-5 text-xs leading-relaxed text-[#777]">Authentication, scan records, and private files are stored in your configured Supabase project.</p>
    </section>
  </main>;

  const mfaRequired = ["owner", "admin"].includes(session.role ?? "") && session.aal !== "aal2";
  const accountMenu = <AccountMenu email={session.user.email} busy={busy} onAccount={() => setAccount(!account)} onSignOut={() => void perform(async () => { await auth("logout", {}); setAccount(false); await refresh(); })} />;
  const showAccount = account || mfaRequired || !session.workspace;
  return <HostedContext.Provider value={{ ...session, accountMenu }}>
    <div className="min-h-screen bg-black text-white">
      <header className="sticky top-0 z-30 flex flex-wrap items-center gap-3 border-b border-[#292929] bg-black px-4 py-3 text-xs sm:px-6 lg:h-16">
        <label className="flex items-center gap-2 text-[#aaa]">Workspace<select aria-label="Active workspace" className="max-w-52 rounded border border-[#333] bg-black px-2 py-2 text-white" value={session.workspace ?? ""} onChange={event => void perform(async () => { await auth("workspace", { workspace: event.target.value }); window.location.hash = "page=pentest"; await refresh(); })}>
          {!session.workspaces?.length && <option value="">No workspace access</option>}{session.workspaces?.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
        </select></label>
        <span className="text-[#888]">{session.role ?? "No membership"}</span>
        <div className="ml-auto lg:hidden">{accountMenu}</div>
      </header>
      {error && <p role="alert" className="px-6 py-3 text-sm text-red-300">{error}</p>}
      {showAccount ? <div className="flex">
        <Sidebar view={null} onSelectView={view => { setAccount(false); window.location.hash = `page=${view}`; }} canManageScans={["owner", "admin"].includes(session.role ?? "") && session.aal === "aal2"} hosted footer={accountMenu} mcpConnections={[]} mcpInUse={new Set()} />
        <div className="min-w-0 flex-1"><AccountPanel key={session.user.id + session.workspace} session={session} refresh={refresh} mfaRequired={mfaRequired} /></div>
      </div> : <div key={session.user.id + session.workspace + session.aal + session.role}>{children}</div>}
    </div>
  </HostedContext.Provider>;
}

function AccountMenu({ email, busy, onAccount, onSignOut }: { email: string; busy: boolean; onAccount: () => void; onSignOut: () => void }) {
  const details = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => {
      if (!details.current?.contains(event.target as Node)) details.current?.removeAttribute("open");
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, []);
  return <details ref={details} className="group relative" onKeyDown={event => {
    if (event.key === "Escape") { details.current?.removeAttribute("open"); details.current?.querySelector("summary")?.focus(); }
  }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) details.current?.removeAttribute("open"); }}>
    <summary aria-label="Account menu" className="flex cursor-pointer list-none items-center gap-3 rounded-lg p-2 text-sm text-[#ccc] hover:bg-white/[0.06] focus-visible:outline-emerald-400 [&::-webkit-details-marker]:hidden">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full border border-emerald-500/30 bg-emerald-500/10 font-medium text-emerald-300" aria-hidden="true">{email.charAt(0).toUpperCase()}</span>
      <span className="hidden flex-1 text-left lg:block">Account</span><ChevronUp className="hidden h-4 w-4 group-open:rotate-180 lg:block" aria-hidden="true" />
    </summary>
    <div className="absolute right-0 top-full z-50 mt-2 w-64 max-w-[calc(100vw-2rem)] rounded-xl border border-[#333] bg-[#111] p-1.5 shadow-2xl lg:bottom-full lg:left-0 lg:right-auto lg:top-auto lg:mb-2 lg:mt-0 lg:w-full lg:min-w-0">
      <p className="break-all border-b border-[#292929] px-3 py-3 text-xs text-[#aaa]">{email}</p>
      <button className="mt-1 flex w-full cursor-pointer items-center gap-2 rounded-md px-3 py-2.5 text-left text-sm text-[#ddd] hover:bg-white/[0.08] focus-visible:outline-emerald-400" onClick={() => { details.current?.removeAttribute("open"); onAccount(); }}><Users className="h-4 w-4 shrink-0" aria-hidden="true" />Account &amp; team</button>
      <button disabled={busy} className="flex w-full cursor-pointer items-center gap-2 rounded-md px-3 py-2.5 text-left text-sm text-[#ddd] hover:bg-white/[0.08] focus-visible:outline-emerald-400 disabled:opacity-50" onClick={onSignOut}><LogOut className="h-4 w-4 shrink-0" aria-hidden="true" />Sign out</button>
    </div>
  </details>;
}

type Factor = { id: string; status: string; factor_type: string; friendly_name?: string };
type Members = { members: { user_id: string; role: string }[]; invitations: { email: string; role: string }[] };

function AccountPanel({ session, refresh, mfaRequired }: { session: HostedSession; refresh: () => Promise<void>; mfaRequired: boolean }) {
  const [factors, setFactors] = useState<Factor[]>([]);
  const [enrollment, setEnrollment] = useState<{ id: string; totp: { qr_code: string; secret: string } } | null>(null);
  const [factor, setFactor] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [name, setName] = useState("");
  const [members, setMembers] = useState<Members | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const admin = ["owner", "admin"].includes(session.role ?? "") && session.aal === "aal2";
  const load = useCallback(async () => {
    const data = await auth("factors"); setFactors(data.factors ?? []);
    setFactor((previous: string) => previous || data.factors?.find((item: Factor) => item.status === "verified")?.id || "");
    if (admin) setMembers(await auth("members"));
  }, [admin]);
  useEffect(() => { void load().catch(e => setError(e.message)); }, [load]);
  async function run(action: () => Promise<void>) {
    setBusy(true); setMessage(null); setError(null);
    try { await action(); await refresh(); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Request failed."); }
    finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-4xl space-y-8 px-5 py-8 sm:py-12">
    <div><h1 className="text-2xl font-semibold">Account &amp; team</h1><p className="mt-3 text-sm text-[#999]">{mfaRequired ? "Administrators must verify MFA before accessing workspace data." : "Manage your sign-in security and workspace access."}</p></div>
    {!session.workspace && <p className="rounded-lg border border-[#333] p-4 text-sm text-[#aaa]">You have no workspace membership. Ask an owner to invite your verified email address, then sign in again.</p>}
    {message && <p role="status" className="text-sm text-emerald-300">{message}</p>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    <section className="space-y-5 rounded-xl border border-[#292929] p-5 sm:p-6"><h2 className="text-lg font-medium">Authenticator app</h2><p className="text-sm text-[#999]">MFA is required for administrators and scan execution. Current session: {session.aal === "aal2" ? "MFA verified" : "Password verified only"}.</p>
      {enrollment && <div className="space-y-3"><img alt="Scan this QR code with your authenticator app" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(enrollment.totp.qr_code)}`} className="h-48 w-48 rounded bg-white p-3" /><p className="break-all font-mono text-xs text-[#bbb]">Manual setup key: {enrollment.totp.secret}</p><p className="text-xs text-[#888]">Keep this key private. Enter a code below to finish enrollment.</p></div>}
      <form className="flex flex-wrap items-end gap-3" onSubmit={event => { event.preventDefault(); void run(async () => { await auth("mfa/verify", { factor_id: enrollment?.id ?? factor, code }); setCode(""); setEnrollment(null); setMessage("MFA verified."); }); }}>
        {!enrollment && factors.some(item => item.status === "verified") && <label className="text-sm text-[#ccc]">Authenticator<select className={input} value={factor} onChange={e => setFactor(e.target.value)}>{factors.filter(item => item.status === "verified").map(item => <option key={item.id} value={item.id}>{item.friendly_name ?? "Authenticator"}</option>)}</select></label>}
        {(enrollment || factors.some(item => item.status === "verified")) && <><label className="text-sm text-[#ccc]">Six-digit code<input className={input} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={e => setCode(e.target.value)} /></label><button className={button} disabled={busy}>Verify MFA</button></>}
      </form>
      {!enrollment && <button className={button} disabled={busy} onClick={() => void run(async () => setEnrollment(await auth("mfa/enroll", {})))}>Set up authenticator</button>}
      {session.aal === "aal2" && factors.filter(item => item.status === "verified").map(item => <button key={item.id} disabled={busy} className={`${button} ml-3`} onClick={() => { if (window.confirm("Remove this authenticator? You will need to sign in again.")) void run(async () => { await auth("mfa/remove", { factor_id: item.id }); window.location.reload(); }); }}>Remove {item.friendly_name ?? "authenticator"}</button>)}
    </section>
    <form className="space-y-4 rounded-xl border border-[#292929] p-5 sm:p-6" onSubmit={event => { event.preventDefault(); void run(async () => { await auth("password", { password }); setPassword(""); setMessage("Password updated. Other dashboard sessions were revoked."); }); }}><h2 className="text-lg font-medium">Change password</h2><label className="block text-sm text-[#ccc]">New password<input type="password" autoComplete="new-password" minLength={12} required className={input} value={password} onChange={e => setPassword(e.target.value)} /></label><button disabled={busy} className={button}>Update password</button></form>
    {admin && <section className="space-y-6 rounded-xl border border-[#292929] p-5 sm:p-6"><h2 className="text-lg font-medium">Workspace members</h2>
      <form className="grid grid-cols-1 items-end gap-3 sm:flex sm:flex-wrap" onSubmit={event => { event.preventDefault(); void run(async () => { await auth("invite", { email, role }); setEmail(""); setMessage("Invitation saved. New users receive an email; existing users accept it on their next sign-in."); }); }}><label className="min-w-0 flex-1 text-sm text-[#ccc]">Email<input className={input} type="email" required value={email} onChange={e => setEmail(e.target.value)} /></label><label className="text-sm text-[#ccc]">Role<select className={input} value={role} onChange={e => setRole(e.target.value)}>{["viewer", "operator", "admin"].map(value => <option key={value}>{value}</option>)}</select></label><button disabled={busy} className={button}>Invite member</button></form>
      <ul className="space-y-3">{members?.members.map(member => <li key={member.user_id} className="flex flex-wrap items-center gap-3 border-t border-[#222] pt-3"><span className="min-w-0 flex-1 break-all font-mono text-xs text-[#aaa]">{member.user_id}{member.user_id === session.user?.id ? " (you)" : ""}</span>{(member.role === "owner" || member.user_id === session.user?.id) && <span className="text-xs text-[#888]">{member.role}</span>}{member.role !== "owner" && member.user_id !== session.user?.id && <><select aria-label={`Role for ${member.user_id}`} className="rounded border border-[#333] bg-black p-2 text-xs" value={member.role} onChange={event => void run(async () => { await auth("member", { user_id: member.user_id, role: event.target.value }); })}>{["viewer", "operator", "admin"].map(value => <option key={value}>{value}</option>)}</select><button disabled={busy} className={button} onClick={() => { if (window.confirm("Remove this member’s workspace access?")) void run(async () => { await auth("member", { user_id: member.user_id }); }); }}>Remove</button>{session.role === "owner" && <button disabled={busy} className={button} onClick={() => { if (window.confirm("Transfer workspace ownership to this member? You will become an admin.")) void run(async () => { await auth("transfer", { user_id: member.user_id }); }); }}>Transfer ownership</button>}</>}</li>)}</ul>
      {members?.invitations.map(invite => <p key={invite.email} className="text-xs text-[#888]">Pending: {invite.email} · {invite.role}</p>)}
      {session.role === "owner" && <form className="space-y-3 border-t border-[#222] pt-5" onSubmit={event => { event.preventDefault(); void run(async () => { await auth("create-workspace", { name }); setName(""); setMessage("Workspace created. Select it above."); }); }}><label className="block text-sm text-[#ccc]">New workspace name<input required maxLength={120} className={input} value={name} onChange={event => setName(event.target.value)} /></label><button disabled={busy} className={button}>Create workspace</button></form>}
    </section>}
  </main>;
}
