-- 006_onchain.sql: on-chain sealing (egoquant/anchoring). PROPOSED by the on-chain agent, approved by Onur (19 Oct
-- 20:00, with the avatar change below). Bideci saves it as supabase/migrations/006_onchain.sql and runs it after
-- 001, 002_hardening, 002_what_if, 003, 004, 005. Idempotent.
-- advisory_records: the canonical record text + its salt + commitment (one per confirmed report version).
-- record_seals: append-only status rows for the Sepolia transaction (the latest row is the status).
-- Both: RLS (own rows), no anon access, column-level insert grants, NO update/delete grants (each status change is
-- a new row), 180-day expiry, erased by delete_my_data()/purge_expired(), cascade on report and account deletion.

create table if not exists public.advisory_records (
    id                uuid primary key default gen_random_uuid(),
    user_id           uuid not null default auth.uid() references auth.users (id) on delete cascade,
    report_id         uuid not null references public.reports (id) on delete cascade,
    record_version    integer not null default 1 check (record_version between 1 and 100),
    schema            text not null check (schema = 'egoq-record-1'),
    record_canonical  text not null check (length(record_canonical) < 65536),
    salt_hex          text not null check (salt_hex ~ '^[0-9a-f]{64}$'),
    commitment_hex    text not null unique check (commitment_hex ~ '^[0-9a-f]{64}$'),
    report_sha256     text not null check (report_sha256 ~ '^[0-9a-f]{64}$'),
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null default now() + interval '180 days',
    unique (report_id, record_version)
);
create index if not exists advisory_records_user on public.advisory_records (user_id, created_at desc);

create table if not exists public.record_seals (
    id               bigserial primary key,
    user_id          uuid not null default auth.uid() references auth.users (id) on delete cascade,
    record_id        uuid not null references public.advisory_records (id) on delete cascade,
    at               timestamptz not null default now(),
    status           text not null check (status in ('pending', 'submitted', 'confirmed', 'failed')),
    chain_id         bigint not null check (chain_id in (11155111)),
    sealing_address  text check (sealing_address ~ '^0x[0-9a-fA-F]{40}$'),
    tx_hash          text check (tx_hash ~ '^0x[0-9a-f]{64}$'),
    block_number     bigint check (block_number >= 0),
    block_time       timestamptz,
    error_kind       text check (error_kind ~ '^[a-z_]{1,32}$'),   -- a label ('rpc_down', 'cap', ...), never a message
    expires_at       timestamptz not null default now() + interval '180 days'
);
create index if not exists record_seals_record_at on public.record_seals (record_id, at desc);

-- privileges: nothing for anon; select + column-level insert for authenticated; no update, no delete
revoke all on public.advisory_records, public.record_seals from anon, authenticated;
grant select on public.advisory_records, public.record_seals to authenticated;
grant insert (user_id, report_id, record_version, schema, record_canonical, salt_hex, commitment_hex, report_sha256)
    on public.advisory_records to authenticated;
grant insert (user_id, record_id, status, chain_id, sealing_address, tx_hash, block_number, block_time, error_kind)
    on public.record_seals to authenticated;
grant usage on all sequences in schema public to authenticated;

-- row-level security: own rows only; inserts must also point at the user's OWN report / record
alter table public.advisory_records enable row level security;
alter table public.record_seals     enable row level security;
drop policy if exists own_rows_select on public.advisory_records;
drop policy if exists own_rows_insert on public.advisory_records;
drop policy if exists own_rows_select on public.record_seals;
drop policy if exists own_rows_insert on public.record_seals;
create policy own_rows_select on public.advisory_records for select to authenticated
    using (user_id = (select auth.uid()));
create policy own_rows_insert on public.advisory_records for insert to authenticated
    with check (user_id = (select auth.uid()) and exists (
        select 1 from public.reports r where r.id = report_id and r.user_id = (select auth.uid())));
create policy own_rows_select on public.record_seals for select to authenticated
    using (user_id = (select auth.uid()));
create policy own_rows_insert on public.record_seals for insert to authenticated
    with check (user_id = (select auth.uid()) and exists (
        select 1 from public.advisory_records a where a.id = record_id and a.user_id = (select auth.uid())));

-- new log actions and consent scope (001's inline checks; constraint names are Postgres' defaults)
alter table public.operation_log drop constraint if exists operation_log_action_check;
alter table public.operation_log add constraint operation_log_action_check check (action in (
    'consent_given', 'data_connected', 'data_deleted', 'report_generated', 'trade_checked', 'warning_shown',
    'trade_recorded', 'decision_recorded',
    'questionnaire_submitted', 'report_viewed', 'plan_confirmed', 'record_sealed', 'seal_pending'));
alter table public.consents drop constraint if exists consents_scope_check;
alter table public.consents add constraint consents_scope_check
    check (scope in ('ibkr_data', 'trade_check', 'record_seal'));

-- a sealed report's news sections are frozen (the seal fingerprints the saved report as it was at confirm)
create or replace function public.replace_report_briefing(rid uuid, sections jsonb) returns void
language plpgsql security definer set search_path = public as $$
declare
    uid uuid := auth.uid();
    k text;
    allowed text[] := array['market_briefing', 'market_briefing_month', 'what_changed', 'what_changed_action',
                            'what_changed_month', 'what_changed_month_action'];
begin
    if uid is null then raise exception 'not signed in'; end if;
    if jsonb_typeof(sections) <> 'object' then raise exception 'sections must be an object'; end if;
    for k in select jsonb_object_keys(sections) loop
        if not (k = any(allowed)) then raise exception 'not an Agent D section: %', k; end if;
    end loop;
    if exists (select 1 from public.advisory_records where report_id = rid and user_id = uid) then
        raise exception 'report is sealed';
    end if;
    update public.reports
       set report = jsonb_set(report, '{report}', coalesce(report -> 'report', '{}'::jsonb) || sections)
     where id = rid and user_id = uid;
    if not found then raise exception 'report not found'; end if;
end $$;
revoke all on function public.replace_report_briefing(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.replace_report_briefing(uuid, jsonb) to authenticated;

-- erase: the user's data (record_seals first; advisory_records also cascade from reports)
create or replace function public.delete_my_data() returns void
language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid();
begin
    if uid is null then raise exception 'not signed in'; end if;
    delete from public.record_seals     where user_id = uid;
    delete from public.advisory_records where user_id = uid;
    delete from public.trades           where user_id = uid;
    delete from public.cash_flows       where user_id = uid;
    delete from public.what_if_runs     where user_id = uid;
    delete from public.privacy_log      where user_id = uid;
    delete from public.reports          where user_id = uid;
    delete from public.trade_checks     where user_id = uid;
    delete from public.consents         where user_id = uid;
    delete from public.operation_log    where user_id = uid;
    update public.profiles set avatar = null where user_id = uid;   -- the picture is personal data too (Onur)
end $$;

create or replace function public.purge_expired() returns void
language plpgsql security definer set search_path = public as $$
begin
    delete from public.record_seals     where expires_at < now();
    delete from public.advisory_records where expires_at < now();
    delete from public.trades           where expires_at < now();
    delete from public.cash_flows       where expires_at < now();
    delete from public.what_if_runs     where expires_at < now();
    delete from public.privacy_log      where expires_at < now();
    delete from public.reports          where expires_at < now();
    delete from public.trade_checks     where expires_at < now();
    delete from public.consents         where expires_at < now();
    delete from public.operation_log    where expires_at < now();
end $$;

revoke all on function public.delete_my_data(), public.purge_expired() from public, anon;
revoke all on function public.purge_expired() from authenticated;
grant execute on function public.delete_my_data() to authenticated;
