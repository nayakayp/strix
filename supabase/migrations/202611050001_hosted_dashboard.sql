-- Apply explicitly with a migration/admin connection, never at application startup.
begin;
create schema if not exists natsuhat;
revoke all on schema natsuhat from public, anon, authenticated;
do $$ begin
  if not exists (select from pg_roles where rolname = 'natsuhat_api') then
    create role natsuhat_api nologin nobypassrls;
  end if;
  if not exists (select from pg_roles where rolname = 'natsuhat_worker') then
    create role natsuhat_worker nologin nobypassrls;
  end if;
end $$;
grant usage on schema natsuhat to natsuhat_api, natsuhat_worker;

create table natsuhat.workspaces (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(name) between 1 and 120),
  created_at timestamptz not null default now()
);
create table natsuhat.workspace_members (
  workspace_id uuid not null references natsuhat.workspaces on delete cascade,
  user_id uuid not null references auth.users on delete cascade,
  role text not null check (role in ('owner', 'admin', 'operator', 'viewer')),
  primary key (workspace_id, user_id)
);
create index on natsuhat.workspace_members(user_id);
create table natsuhat.workspace_invitations (
  workspace_id uuid not null references natsuhat.workspaces on delete cascade,
  email text not null check (email = lower(email)),
  role text not null check (role in ('admin', 'operator', 'viewer')),
  expires_at timestamptz not null default now() + interval '7 days',
  primary key (workspace_id, email)
);
create table natsuhat.app_sessions (
  id text primary key,
  user_id uuid not null references auth.users on delete cascade,
  workspace_id uuid references natsuhat.workspaces on delete set null,
  encrypted_tokens text not null,
  expires_at timestamptz not null default now() + interval '7 days'
);
create index on natsuhat.app_sessions(expires_at);
create table natsuhat.scans (
  workspace_id uuid not null references natsuhat.workspaces on delete cascade,
  id text not null check (id ~ '^[a-zA-Z0-9_-]{1,160}$'),
  job jsonb not null,
  runner jsonb not null default '{}',
  instructions text not null default '',
  created_by uuid references auth.users on delete set null,
  created_at timestamptz not null default now(),
  deleted_at timestamptz,
  primary key (workspace_id, id)
);
create index on natsuhat.scans(workspace_id, created_at desc);
create table natsuhat.scan_snapshots (
  workspace_id uuid not null,
  scan_id text not null,
  generation text not null,
  payload jsonb not null,
  object_key text not null,
  sha256 text not null,
  size bigint not null check (size >= 0),
  published_at timestamptz not null default now(),
  primary key (workspace_id, scan_id),
  foreign key (workspace_id, scan_id) references natsuhat.scans on delete cascade
);
create table natsuhat.findings (
  workspace_id uuid not null,
  scan_id text not null,
  id text not null,
  title text not null,
  severity text not null,
  payload jsonb not null,
  triage jsonb not null default '{}',
  primary key (workspace_id, scan_id, id),
  foreign key (workspace_id, scan_id) references natsuhat.scans on delete cascade
);
create index on natsuhat.findings(workspace_id, severity);
create table natsuhat.source_snapshots (
  workspace_id uuid not null references natsuhat.workspaces on delete cascade,
  scan_id text not null,
  object_key text not null,
  sha256 text not null,
  size bigint not null check (size >= 0),
  metadata jsonb not null,
  expires_at timestamptz default now() + interval '24 hours',
  primary key (workspace_id, scan_id)
);
-- Register before uploading; an interrupted publication must not leak private objects.
create table natsuhat.storage_objects (
  bucket text not null check (bucket in ('scan-artifacts', 'scan-sources')),
  object_key text not null,
  delete_after timestamptz default now() + interval '24 hours',
  primary key (bucket, object_key)
);
create table natsuhat.audit_events (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references natsuhat.workspaces on delete cascade,
  user_id uuid references auth.users on delete set null,
  action text not null,
  resource text,
  created_at timestamptz not null default now()
);

-- Membership lookup avoids recursive membership RLS. No caller-provided user ID.
create function natsuhat.member_role(wid uuid) returns text
language sql stable security definer set search_path = '' as $$
  select role from natsuhat.workspace_members
  where workspace_id = wid
    and user_id = nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
revoke all on function natsuhat.member_role(uuid) from public;
grant execute on function natsuhat.member_role(uuid) to natsuhat_api;

grant select on all tables in schema natsuhat to natsuhat_api;
revoke all on natsuhat.app_sessions from natsuhat_api;
grant select, insert, update, delete on all tables in schema natsuhat to natsuhat_worker;
do $$ declare t text; begin
  foreach t in array array['workspaces','workspace_members','workspace_invitations',
    'app_sessions','scans','scan_snapshots','findings','source_snapshots','audit_events',
    'storage_objects'] loop
    execute format('alter table natsuhat.%I enable row level security', t);
    execute format('create policy worker_access on natsuhat.%I to natsuhat_worker using (true) with check (true)', t);
    if t not in ('app_sessions', 'workspaces', 'storage_objects') then
      execute format('create policy member_read on natsuhat.%I for select to natsuhat_api using (natsuhat.member_role(workspace_id) is not null)', t);
    end if;
  end loop;
end $$;
create policy member_read on natsuhat.workspaces for select to natsuhat_api
  using (natsuhat.member_role(id) is not null);
-- Source metadata is more sensitive than result viewing.
drop policy member_read on natsuhat.source_snapshots;
create policy operator_read on natsuhat.source_snapshots for select to natsuhat_api
  using (natsuhat.member_role(workspace_id) in ('owner','admin','operator'));
drop policy member_read on natsuhat.workspace_invitations;
create policy admin_read on natsuhat.workspace_invitations for select to natsuhat_api
  using (natsuhat.member_role(workspace_id) in ('owner','admin'));
revoke all on natsuhat.storage_objects from natsuhat_api;

-- Private buckets. No browser policies: file operations go through the backend.
insert into storage.buckets (id, name, public)
values ('scan-artifacts', 'scan-artifacts', false), ('scan-sources', 'scan-sources', false)
on conflict (id) do update set public = false;
commit;
