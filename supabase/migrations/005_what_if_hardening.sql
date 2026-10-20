-- EgoQuant: privilege hardening for the two What-If tables (what_if_runs, privacy_log). Run in the Supabase SQL editor.
-- Run order: 001_accounts, 002_hardening, 002_what_if, 003_avatar, 004_report_briefing, then 005_onboarding, 005 (this file) and
-- 006_onchain in ANY order (this file only touches grants), and 007_erase_everything LAST. Idempotent. Reviewed by Onur on 20 Oct;
-- final, no longer a draft.
--
-- Why: 002_what_if.sql only revokes from anon and grants select+insert to authenticated. Supabase's default privileges
-- already granted ALL (including UPDATE, DELETE, TRUNCATE) on new public tables to authenticated, and TRUNCATE ignores
-- RLS. Also an insert could set created_at / at / expires_at. Same fix as 002_hardening.sql, for the two new tables.
-- The backend inserts exactly these columns (egoquant/what_if_api.py store_run, chat.py _store_rows):
--   what_if_runs: user_id, report_id, context_hash, scenario, summary, status, seed, n_paths, engine_version
--   privacy_log : user_id, session_id, counts
-- Nothing updates or deletes these rows except delete_my_data() / purge_expired() (security definer).

revoke all on public.what_if_runs, public.privacy_log from anon, authenticated;

grant select on public.what_if_runs, public.privacy_log to authenticated;
grant insert (user_id, report_id, context_hash, scenario, summary, status, seed, n_paths, engine_version)
    on public.what_if_runs to authenticated;
grant insert (user_id, session_id, counts) on public.privacy_log to authenticated;

grant usage on all sequences in schema public to authenticated;     -- privacy_log.id is a bigserial

-- Re-assert the function grants (002_what_if.sql replaced the bodies; CREATE OR REPLACE keeps the old grants, but be explicit).
revoke all on function public.delete_my_data(), public.delete_my_account(), public.purge_expired()
    from public, anon, authenticated;
grant execute on function public.delete_my_data(), public.delete_my_account() to authenticated;

-- Check (should list ONLY select for authenticated on the two tables, and nothing for anon):
--   select grantee, table_name, privilege_type from information_schema.role_table_grants
--    where table_schema = 'public' and table_name in ('what_if_runs','privacy_log') and grantee in ('anon','authenticated')
--    order by 2, 1, 3;
--   select grantee, table_name, column_name, privilege_type from information_schema.column_privileges
--    where table_schema = 'public' and table_name in ('what_if_runs','privacy_log') and grantee = 'authenticated' and privilege_type = 'INSERT';
