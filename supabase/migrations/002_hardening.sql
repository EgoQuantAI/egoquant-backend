-- EgoQuant accounts: privilege hardening on top of 001_accounts.sql. Run in the Supabase SQL editor after 001.
-- Idempotent: safe to run again. Renames or drops nothing; every call the backend makes keeps working.
--
-- Why
--   * Supabase's default privileges grant ALL on new public tables (and EXECUTE on new functions) to anon and
--     authenticated. 001 revokes anon but only ADDS grants for authenticated, so authenticated kept
--     DELETE/UPDATE/TRUNCATE/REFERENCES/TRIGGER everywhere and EXECUTE on purge_expired(). RLS blocks row
--     access without a policy, but TRUNCATE ignores RLS. Here: revoke everything, grant back exactly what is used.
--   * trade_checks: the user may only record their decision (user_decision, decided_at), never rewrite the
--     verdict, the result or the planned trade (they come from deterministic code).
--   * profiles: only the username may change; user_id and created_at are fixed.
--   * Inserts cannot set created_at / given_at / at / expires_at: retention (180 days) and the operation log's
--     timestamps always come from the database defaults.

-- ---------------------------------------------------------------- tables
revoke all on public.profiles, public.consents, public.trades, public.cash_flows, public.reports,
              public.trade_checks, public.operation_log from anon, authenticated;

grant select on public.profiles, public.consents, public.trades, public.cash_flows, public.reports,
                public.trade_checks, public.operation_log to authenticated;

-- profiles rows are created by the sign-up trigger (security definer), never by the user.
grant update (username) on public.profiles to authenticated;

grant insert (user_id, scope, version) on public.consents to authenticated;
grant insert (user_id, trade_date, symbol, side, quantity, price, currency, asset_category, cash_change, source)
    on public.trades to authenticated;
grant insert (user_id, day, kind, amount, currency, source) on public.cash_flows to authenticated;
grant insert (user_id, summary, report) on public.reports to authenticated;
grant insert (user_id, planned, verdict, result) on public.trade_checks to authenticated;
grant insert (user_id, action, details) on public.operation_log to authenticated;

grant delete on public.trades, public.cash_flows, public.reports to authenticated;
grant update (user_decision, decided_at) on public.trade_checks to authenticated;

grant usage on all sequences in schema public to authenticated;

-- ---------------------------------------------------------------- functions
revoke all on function public.delete_my_data(), public.delete_my_account(), public.purge_expired(),
                       public.handle_new_user(), public.username_available(text) from public, anon, authenticated;
grant execute on function public.delete_my_data(), public.delete_my_account() to authenticated;
grant execute on function public.username_available(text) to anon, authenticated;

-- ---------------------------------------------------------------- daily purge
-- 001 already tries this; repeated here so enabling pg_cron later and re-running this file is enough.
do $$
begin
    perform cron.schedule('egoquant-purge-expired', '17 3 * * *', 'select public.purge_expired()');
    raise notice 'pg_cron: egoquant-purge-expired scheduled daily at 03:17 UTC';
exception when others then
    raise notice 'pg_cron not available: run select public.purge_expired(); daily by hand';
end $$;
