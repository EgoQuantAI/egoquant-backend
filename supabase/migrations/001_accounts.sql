-- EgoQuant user accounts: profiles, consents, IBKR trading data, reports, trade checks, operation log.
-- Run once in the Supabase SQL editor (Savas). Idempotent where Postgres allows it.
--
-- Security model
--   * Supabase Auth owns sign-up, passwords, email confirmation and login rate limits. No password ever
--     reaches our code or these tables.
--   * Every table has Row-Level Security: a signed-in user reads and writes ONLY rows whose user_id is
--     their own auth.uid(). The backend calls PostgREST with the USER'S token (never a service key), so
--     these policies apply to every request, on top of the backend's own user_id filter.
--   * The anonymous role gets no table access at all.
--   * Data minimisation: trades keep date, symbol, side, quantity, price, currency, asset category and net
--     cash. No account numbers, names or descriptions. IBKR tokens and uploaded files are never stored.
--   * Retention: every user-data row expires after 180 days (expires_at); purge_expired() deletes them.
--   * delete_my_data() / delete_my_account() let the user erase everything themselves.

create extension if not exists citext;

-- ---------------------------------------------------------------- tables
create table if not exists public.profiles (
    user_id     uuid primary key references auth.users (id) on delete cascade,
    username    citext not null unique check (username ~ '^[a-z0-9_]{3,20}$'),
    created_at  timestamptz not null default now()
);

create table if not exists public.consents (
    id          bigserial primary key,
    user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
    scope       text not null check (scope in ('ibkr_data', 'trade_check')),
    version     text not null check (length(version) <= 32),
    given_at    timestamptz not null default now(),
    expires_at  timestamptz not null default now() + interval '180 days'
);

create table if not exists public.trades (
    id              bigserial primary key,
    user_id         uuid not null default auth.uid() references auth.users (id) on delete cascade,
    trade_date      date not null,
    symbol          text not null check (symbol ~ '^[A-Za-z0-9.\-]{1,20}$'),
    side            text not null check (side in ('buy', 'sell')),
    quantity        numeric not null check (quantity > 0),
    price           numeric not null check (price > 0),
    currency        text not null check (currency ~ '^[A-Z]{3}$'),
    asset_category  text not null check (length(asset_category) <= 16),
    cash_change     numeric not null,
    source          text not null check (source in ('ibkr_report', 'ibkr_flex', 'user_recorded')),
    created_at      timestamptz not null default now(),
    expires_at      timestamptz not null default now() + interval '180 days'
);
create index if not exists trades_user_date on public.trades (user_id, trade_date);

create table if not exists public.cash_flows (
    id          bigserial primary key,
    user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
    day         date not null,
    kind        text not null check (length(kind) <= 24),
    amount      numeric not null,
    currency    text not null check (currency ~ '^[A-Z]{3}$'),
    source      text not null check (source in ('ibkr_report', 'ibkr_flex')),
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null default now() + interval '180 days'
);
create index if not exists cash_flows_user_day on public.cash_flows (user_id, day);

create table if not exists public.reports (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
    created_at  timestamptz not null default now(),
    summary     jsonb not null default '{}'::jsonb check (pg_column_size(summary) < 16384),
    report      jsonb not null check (pg_column_size(report) < 2000000),
    expires_at  timestamptz not null default now() + interval '180 days'
);
create index if not exists reports_user_created on public.reports (user_id, created_at desc);

create table if not exists public.trade_checks (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null default auth.uid() references auth.users (id) on delete cascade,
    created_at     timestamptz not null default now(),
    planned        jsonb not null check (pg_column_size(planned) < 4096),
    verdict        text not null check (verdict in ('advisable', 'allowed_with_warnings', 'not_advisable')),
    result         jsonb not null check (pg_column_size(result) < 65536),
    user_decision  text check (user_decision in ('made', 'not_made')),
    decided_at     timestamptz,
    expires_at     timestamptz not null default now() + interval '180 days'
);

create table if not exists public.operation_log (
    id          bigserial primary key,
    user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
    at          timestamptz not null default now(),
    action      text not null check (action in (
                    'consent_given', 'data_connected', 'data_deleted', 'report_generated', 'trade_checked',
                    'warning_shown', 'trade_recorded', 'decision_recorded')),
    details     jsonb not null default '{}'::jsonb check (pg_column_size(details) < 4096),
    expires_at  timestamptz not null default now() + interval '180 days'
);
create index if not exists operation_log_user_at on public.operation_log (user_id, at desc);

-- ---------------------------------------------------------------- privileges
revoke all on public.profiles, public.consents, public.trades, public.cash_flows, public.reports,
              public.trade_checks, public.operation_log from anon;
grant select, insert, update on public.profiles to authenticated;
grant select, insert on public.consents, public.operation_log to authenticated;
grant select, insert, delete on public.trades, public.cash_flows, public.reports to authenticated;
grant select, insert, update on public.trade_checks to authenticated;
grant usage on all sequences in schema public to authenticated;

-- ---------------------------------------------------------------- row-level security
alter table public.profiles      enable row level security;
alter table public.consents      enable row level security;
alter table public.trades        enable row level security;
alter table public.cash_flows    enable row level security;
alter table public.reports       enable row level security;
alter table public.trade_checks  enable row level security;
alter table public.operation_log enable row level security;

do $$
declare t text;
begin
    foreach t in array array['profiles', 'consents', 'trades', 'cash_flows', 'reports', 'trade_checks',
                             'operation_log'] loop
        execute format('drop policy if exists own_rows_select on public.%I', t);
        execute format('drop policy if exists own_rows_insert on public.%I', t);
        execute format('create policy own_rows_select on public.%I for select to authenticated '
                       'using (user_id = (select auth.uid()))', t);
        execute format('create policy own_rows_insert on public.%I for insert to authenticated '
                       'with check (user_id = (select auth.uid()))', t);
    end loop;
    foreach t in array array['trades', 'cash_flows', 'reports'] loop
        execute format('drop policy if exists own_rows_delete on public.%I', t);
        execute format('create policy own_rows_delete on public.%I for delete to authenticated '
                       'using (user_id = (select auth.uid()))', t);
    end loop;
    foreach t in array array['profiles', 'trade_checks'] loop
        execute format('drop policy if exists own_rows_update on public.%I', t);
        execute format('create policy own_rows_update on public.%I for update to authenticated '
                       'using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()))', t);
    end loop;
end $$;

-- ---------------------------------------------------------------- functions
-- The username is chosen at sign-up (user metadata "username") and created here. A taken or invalid name
-- makes the sign-up itself fail, so every account has exactly one unique username.
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
    insert into public.profiles (user_id, username)
    values (new.id, lower(new.raw_user_meta_data ->> 'username'));
    return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
    for each row execute function public.handle_new_user();

-- Whether a username is free (true/false only; never who holds it). Used by the sign-up form.
create or replace function public.username_available(name text) returns boolean
language sql stable security definer set search_path = public as $$
    select name ~ '^[a-z0-9_]{3,20}$' and not exists (select 1 from public.profiles where username = name)
$$;

-- Delete everything the signed-in user has stored (the account itself stays).
create or replace function public.delete_my_data() returns void
language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid();
begin
    if uid is null then raise exception 'not signed in'; end if;
    delete from public.trades        where user_id = uid;
    delete from public.cash_flows    where user_id = uid;
    delete from public.reports       where user_id = uid;
    delete from public.trade_checks  where user_id = uid;
    delete from public.consents      where user_id = uid;
    delete from public.operation_log where user_id = uid;
end $$;

-- Delete the account and, through ON DELETE CASCADE, every row of it.
create or replace function public.delete_my_account() returns void
language plpgsql security definer set search_path = public, auth as $$
declare uid uuid := auth.uid();
begin
    if uid is null then raise exception 'not signed in'; end if;
    delete from auth.users where id = uid;
end $$;

-- Retention: remove every expired row (run daily by pg_cron below, or by hand).
create or replace function public.purge_expired() returns void
language plpgsql security definer set search_path = public as $$
begin
    delete from public.trades        where expires_at < now();
    delete from public.cash_flows    where expires_at < now();
    delete from public.reports       where expires_at < now();
    delete from public.trade_checks  where expires_at < now();
    delete from public.consents      where expires_at < now();
    delete from public.operation_log where expires_at < now();
end $$;

revoke all on function public.delete_my_data(), public.delete_my_account(), public.purge_expired(),
                       public.handle_new_user() from public, anon;
grant execute on function public.delete_my_data(), public.delete_my_account() to authenticated;
grant execute on function public.username_available(text) to anon, authenticated;

-- Daily purge, if the pg_cron extension is enabled (Database -> Extensions). Skipped quietly otherwise.
do $$
begin
    perform cron.schedule('egoquant-purge-expired', '17 3 * * *', 'select public.purge_expired()');
exception when others then
    raise notice 'pg_cron not available: run select public.purge_expired(); daily by hand';
end $$;
