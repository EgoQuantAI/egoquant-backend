-- EgoQuant accounts: ONE final definition of the erase and retention functions. Run LAST, after 001-006.
-- Idempotent. Adds no table and changes no policy or privilege on a table.
--
-- Why this file exists. 002_what_if.sql, 005_onboarding.sql and 006_onchain.sql each replace delete_my_data() and
-- purge_expired() with a version that lists only the tables known when that file was written, and the file run last
-- wins. Run in the order 005, 006: the onboarding answers survive "erase my data". Run 006, 005: the advisory records,
-- the seals and the profile picture survive. This file lists EVERY table that holds a user's rows, skips a table whose
-- migration has not been run (to_regclass), and is the version in force when it is run last. If a later migration adds a
-- table with a user_id, add it here too: tests/accounts/test_migrations.py fails until it is listed.
--
-- What "erase my data" keeps: the account itself (auth user and the profiles row with the user name), so the person can
-- still sign in and start again. delete_my_account() removes the auth user, and every table cascades from it.

create or replace function public.delete_my_data() returns void
language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid();
begin
    if uid is null then raise exception 'not signed in'; end if;
    -- on-chain tables first (record_seals -> advisory_records -> reports)
    if to_regclass('public.record_seals') is not null then
        execute 'delete from public.record_seals where user_id = $1' using uid;
    end if;
    if to_regclass('public.advisory_records') is not null then
        execute 'delete from public.advisory_records where user_id = $1' using uid;
    end if;
    delete from public.trades        where user_id = uid;
    delete from public.cash_flows    where user_id = uid;
    if to_regclass('public.what_if_runs') is not null then
        execute 'delete from public.what_if_runs where user_id = $1' using uid;
    end if;
    if to_regclass('public.privacy_log') is not null then
        execute 'delete from public.privacy_log where user_id = $1' using uid;
    end if;
    delete from public.reports       where user_id = uid;
    delete from public.trade_checks  where user_id = uid;
    delete from public.consents      where user_id = uid;
    delete from public.operation_log where user_id = uid;
    if to_regclass('public.onboarding') is not null then
        execute 'delete from public.onboarding where user_id = $1' using uid;
    end if;
    -- the picture is personal data too (column added by 003_avatar.sql)
    if exists (select 1 from information_schema.columns
               where table_schema = 'public' and table_name = 'profiles' and column_name = 'avatar') then
        execute 'update public.profiles set avatar = null where user_id = $1' using uid;
    end if;
end $$;

create or replace function public.purge_expired() returns void
language plpgsql security definer set search_path = public as $$
begin
    if to_regclass('public.record_seals') is not null then
        execute 'delete from public.record_seals where expires_at < now()';
    end if;
    if to_regclass('public.advisory_records') is not null then
        execute 'delete from public.advisory_records where expires_at < now()';
    end if;
    delete from public.trades        where expires_at < now();
    delete from public.cash_flows    where expires_at < now();
    if to_regclass('public.what_if_runs') is not null then
        execute 'delete from public.what_if_runs where expires_at < now()';
    end if;
    if to_regclass('public.privacy_log') is not null then
        execute 'delete from public.privacy_log where expires_at < now()';
    end if;
    delete from public.reports       where expires_at < now();
    delete from public.trade_checks  where expires_at < now();
    delete from public.consents      where expires_at < now();
    delete from public.operation_log where expires_at < now();
    if to_regclass('public.onboarding') is not null then
        execute 'delete from public.onboarding where expires_at < now()';
    end if;
end $$;

-- Same privileges as 005_onboarding.sql and 006_onchain.sql: the user may erase their own data; only the owner (pg_cron, the
-- SQL editor) may purge.
revoke all on function public.delete_my_data(), public.purge_expired() from public, anon, authenticated;
grant execute on function public.delete_my_data() to authenticated;

-- Check (run after this file; both must say true, and the second list must be empty):
--   select pg_get_functiondef('public.delete_my_data()'::regprocedure) like '%onboarding%'
--      and pg_get_functiondef('public.delete_my_data()'::regprocedure) like '%record_seals%' as erase_covers_both;
--   select grantee, routine_name from information_schema.routine_privileges
--    where routine_schema = 'public' and routine_name = 'purge_expired' and grantee in ('anon', 'authenticated', 'public');
