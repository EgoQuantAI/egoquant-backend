-- EgoQuant accounts: the onboarding answers live in the account, not only in the browser.
-- Run once in the Supabase SQL editor after 001-004. Idempotent. Adds one table and replaces delete_my_data() and
-- purge_expired() so they cover it; nothing is renamed or removed.
--
-- What is stored (one row per user, replaced on every step)
--   The questionnaire answers and the profile they scored to, the investment preferences, the goal inputs
--   (intake), the Risk Game result, when the portfolio was built, and a pointer to the saved report (reports.id)
--   with its sealed advisor contexts. No IBKR data (that stays in trades / cash_flows, behind its own consent),
--   no email, no password. The backend checks the shape and the size (egoquant/accounts/routes.py).
--   The backend never trusts this row for scoring: /portfolio re-scores the answers every time.
--
-- Same security model as 001: Row-Level Security (a user reads and writes ONLY their own row), no anonymous
-- access, 180-day retention, erased by delete_my_data() / delete_my_account().

create table if not exists public.onboarding (
    user_id     uuid primary key default auth.uid() references auth.users (id) on delete cascade,
    state       jsonb not null default '{}'::jsonb check (pg_column_size(state) < 262144),
    updated_at  timestamptz not null default now(),
    expires_at  timestamptz not null default now() + interval '180 days'
);

-- ---------------------------------------------------------------- privileges
revoke all on public.onboarding from anon, authenticated;
grant select on public.onboarding to authenticated;
grant insert (user_id, state) on public.onboarding to authenticated;
grant update (state, updated_at) on public.onboarding to authenticated;

-- ---------------------------------------------------------------- row-level security
alter table public.onboarding enable row level security;

drop policy if exists own_rows_select on public.onboarding;
drop policy if exists own_rows_insert on public.onboarding;
drop policy if exists own_rows_update on public.onboarding;
create policy own_rows_select on public.onboarding for select to authenticated
    using (user_id = (select auth.uid()));
create policy own_rows_insert on public.onboarding for insert to authenticated
    with check (user_id = (select auth.uid()));
create policy own_rows_update on public.onboarding for update to authenticated
    using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

-- ---------------------------------------------------------------- erase and retention (replace 002's versions)
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
    delete from public.onboarding    where user_id = uid;
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
    delete from public.onboarding    where expires_at < now();
end $$;

revoke all on function public.delete_my_data(), public.purge_expired() from public, anon, authenticated;
grant execute on function public.delete_my_data() to authenticated;
