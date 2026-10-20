-- What-If Agent audit log and privacy-panel counts. Run once in the Supabase SQL editor after 001_accounts.sql.
-- Same security model as 001: Row-Level Security on every table (a signed-in user reads and inserts ONLY their own
-- rows; the backend calls PostgREST with the USER'S token), no anonymous access, 180-day retention, and both tables
-- are erased by delete_my_data() / delete_my_account().
--
-- Data minimisation
--   * what_if_runs: the normalised scenario (the change the user asked for), a result summary (status, "X in 100"
--     baseline and scenario, whether the portfolio changed, the Risk Engine verdict), seed, path count, engine
--     version, and a 16-character fingerprint of the sealed base context. No questionnaire answers, no weights,
--     no free text.
--   * privacy_log: COUNTS of values hidden from the AI per chat session and kind (account, email, phone, name).
--     Never the values.

create table if not exists public.what_if_runs (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null default auth.uid() references auth.users (id) on delete cascade,
    created_at      timestamptz not null default now(),
    report_id       uuid references public.reports (id) on delete set null,
    context_hash    text not null check (context_hash ~ '^[0-9a-f]{16}$'),
    scenario        jsonb not null default '{}'::jsonb check (pg_column_size(scenario) < 4096),
    summary         jsonb not null default '{}'::jsonb check (pg_column_size(summary) < 4096),
    status          text not null check (status in ('ok', 'blocked', 'invalid', 'unavailable')),
    seed            bigint,
    n_paths         integer check (n_paths is null or n_paths between 1 and 100000),
    engine_version  text not null check (length(engine_version) <= 32),
    expires_at      timestamptz not null default now() + interval '180 days'
);
create index if not exists what_if_runs_user_created on public.what_if_runs (user_id, created_at desc);

create table if not exists public.privacy_log (
    id          bigserial primary key,
    user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
    session_id  text not null check (session_id ~ '^[A-Za-z0-9-]{8,64}$'),
    at          timestamptz not null default now(),
    counts      jsonb not null default '{}'::jsonb check (pg_column_size(counts) < 1024),
    expires_at  timestamptz not null default now() + interval '180 days'
);
create index if not exists privacy_log_user_session on public.privacy_log (user_id, session_id);

-- ---------------------------------------------------------------- privileges
revoke all on public.what_if_runs, public.privacy_log from anon;
grant select, insert on public.what_if_runs, public.privacy_log to authenticated;
grant usage on all sequences in schema public to authenticated;

-- ---------------------------------------------------------------- row-level security
alter table public.what_if_runs enable row level security;
alter table public.privacy_log  enable row level security;

do $$
declare t text;
begin
    foreach t in array array['what_if_runs', 'privacy_log'] loop
        execute format('drop policy if exists own_rows_select on public.%I', t);
        execute format('drop policy if exists own_rows_insert on public.%I', t);
        execute format('create policy own_rows_select on public.%I for select to authenticated '
                       'using (user_id = (select auth.uid()))', t);
        execute format('create policy own_rows_insert on public.%I for insert to authenticated '
                       'with check (user_id = (select auth.uid()))', t);
    end loop;
end $$;

-- ---------------------------------------------------------------- erase and retention (replace 001's versions)
create or replace function public.delete_my_data() returns void
language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid();
begin
    if uid is null then raise exception 'not signed in'; end if;
    delete from public.trades        where user_id = uid;
    delete from public.cash_flows    where user_id = uid;
    delete from public.what_if_runs  where user_id = uid;
    delete from public.privacy_log   where user_id = uid;
    delete from public.reports       where user_id = uid;
    delete from public.trade_checks  where user_id = uid;
    delete from public.consents      where user_id = uid;
    delete from public.operation_log where user_id = uid;
end $$;

create or replace function public.purge_expired() returns void
language plpgsql security definer set search_path = public as $$
begin
    delete from public.trades        where expires_at < now();
    delete from public.cash_flows    where expires_at < now();
    delete from public.what_if_runs  where expires_at < now();
    delete from public.privacy_log   where expires_at < now();
    delete from public.reports       where expires_at < now();
    delete from public.trade_checks  where expires_at < now();
    delete from public.consents      where expires_at < now();
    delete from public.operation_log where expires_at < now();
end $$;

revoke all on function public.delete_my_data(), public.purge_expired() from public, anon;
grant execute on function public.delete_my_data() to authenticated;
