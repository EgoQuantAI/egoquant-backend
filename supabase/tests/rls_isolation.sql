-- Row-Level Security proof: user A can never read or change user B's rows. Run in the Supabase SQL editor
-- AFTER migrations/001_accounts.sql, 002_hardening.sql and 003_avatar.sql.
-- Everything happens inside one transaction that is
-- ROLLED BACK at the end, so no test user or row is left behind. Any failure raises an exception naming the
-- broken rule; success ends with one row "RLS isolation: all checks passed".

begin;

-- Two throwaway users (the sign-up trigger gives each a profile from the "username" metadata).
insert into auth.users (id, email, raw_user_meta_data, aud, role)
values ('00000000-0000-0000-0000-00000000000a', 'rls-a@example.invalid', '{"username": "rls_test_a"}', 'authenticated', 'authenticated'),
       ('00000000-0000-0000-0000-00000000000b', 'rls-b@example.invalid', '{"username": "rls_test_b"}', 'authenticated', 'authenticated');

-- Act as user A: one row in every table (the profile came from the trigger).
set local role authenticated;
select set_config('request.jwt.claims', '{"sub": "00000000-0000-0000-0000-00000000000a", "role": "authenticated"}', true);
insert into public.consents (scope, version) values ('ibkr_data', 'test');
insert into public.trades (trade_date, symbol, side, quantity, price, currency, asset_category, cash_change, source)
values ('2026-01-05', 'VTI', 'buy', 10, 250, 'USD', 'STK', -2500, 'ibkr_report');
insert into public.cash_flows (day, kind, amount, currency, source) values ('2026-01-02', 'deposit', 5000, 'USD', 'ibkr_report');
insert into public.reports (report) values ('{"test": true}');
insert into public.trade_checks (planned, verdict, result)
values ('{"ticker": "VTI", "side": "buy"}', 'not_advisable', '{"test": true}');
insert into public.operation_log (action) values ('consent_given');

do $$
declare
    t text;
    n bigint;
begin
    -- A sees exactly one own row in each table.
    foreach t in array array['profiles', 'consents', 'trades', 'cash_flows', 'reports', 'trade_checks', 'operation_log'] loop
        execute format('select count(*) from public.%I', t) into n;
        if n <> 1 then raise exception 'SETUP: A should see 1 row in %, sees %', t, n; end if;
    end loop;

    -- A cannot write a row in B's name.
    begin
        insert into public.reports (user_id, report) values ('00000000-0000-0000-0000-00000000000b', '{}');
        raise exception 'RLS BROKEN: A inserted a row for B';
    exception when insufficient_privilege or check_violation then null;
    end;

    -- A cannot choose their own retention date (002: column-level insert grants).
    begin
        insert into public.reports (report, expires_at) values ('{}', now() + interval '10 years');
        raise exception 'PRIVILEGE BROKEN: A set expires_at on insert';
    exception when insufficient_privilege then null;
    end;

    -- A cannot rewrite the verdict of their own trade check, only record the decision (002).
    begin
        update public.trade_checks set verdict = 'advisable';
        raise exception 'PRIVILEGE BROKEN: A rewrote a trade-check verdict';
    exception when insufficient_privilege then null;
    end;
    update public.trade_checks set user_decision = 'not_made', decided_at = now();
    if (select user_decision from public.trade_checks) is distinct from 'not_made' then
        raise exception 'SETUP: A could not record a decision';
    end if;

    -- A cannot change their profile's owner (002: only username is updatable).
    begin
        update public.profiles set user_id = '00000000-0000-0000-0000-00000000000b';
        raise exception 'PRIVILEGE BROKEN: A changed profiles.user_id';
    exception when insufficient_privilege then null;
    end;

    -- A can set their own picture (003), but only a preset or a raster data URL.
    update public.profiles set avatar = 'preset:p2';
    begin
        update public.profiles set avatar = 'data:image/svg+xml;base64,PHN2Zz4=';
        raise exception 'CHECK BROKEN: an SVG avatar was accepted';
    exception when check_violation then null;
    end;

    -- Retention purge is for the database (pg_cron / admin), not for signed-in users (002).
    begin
        perform public.purge_expired();
        raise exception 'PRIVILEGE BROKEN: authenticated can run purge_expired()';
    exception when insufficient_privilege then null;
    end;
end $$;

-- Act as user B: sees nothing of A's, can change or delete nothing of A's.
select set_config('request.jwt.claims', '{"sub": "00000000-0000-0000-0000-00000000000b", "role": "authenticated"}', true);
do $$
declare
    t text;
    n bigint;
begin
    foreach t in array array['profiles', 'consents', 'trades', 'cash_flows', 'reports', 'trade_checks', 'operation_log'] loop
        execute format('select count(*) from public.%I where user_id <> (select auth.uid())', t) into n;
        if n <> 0 then raise exception 'RLS BROKEN: B sees % of A''s rows in %', n, t; end if;
    end loop;
    if (select count(*) from public.profiles) <> 1 then raise exception 'SETUP: B should see exactly its own profile'; end if;
end $$;
delete from public.trades;                                      -- RLS: affects only B's own (zero) rows
delete from public.cash_flows;
delete from public.reports;
update public.profiles set username = 'stolen_name';            -- RLS: affects only B's own profile
update public.profiles set avatar = 'preset:p5';                -- RLS: affects only B's own picture
update public.trade_checks set user_decision = 'made';          -- RLS: B has no checks, nothing changes
select public.delete_my_data();                                 -- erases B's rows only

-- Back to A: everything is still there and unchanged.
select set_config('request.jwt.claims', '{"sub": "00000000-0000-0000-0000-00000000000a", "role": "authenticated"}', true);
do $$
declare
    t text;
    n bigint;
begin
    foreach t in array array['consents', 'trades', 'cash_flows', 'reports', 'trade_checks', 'operation_log'] loop
        execute format('select count(*) from public.%I', t) into n;
        if n <> 1 then raise exception 'RLS BROKEN: A''s % changed while acting as B (% rows)', t, n; end if;
    end loop;
    if (select username from public.profiles) <> 'rls_test_a' then raise exception 'RLS BROKEN: B renamed A'; end if;
    if (select avatar from public.profiles) is distinct from 'preset:p2' then raise exception 'RLS BROKEN: B changed A''s picture'; end if;
    if (select user_decision from public.trade_checks) <> 'not_made' then raise exception 'RLS BROKEN: B decided on A''s check'; end if;
end $$;

-- The anonymous role reads no table and can only ask whether a username is free.
reset role;
set local role anon;
do $$
declare t text;
begin
    foreach t in array array['profiles', 'consents', 'trades', 'cash_flows', 'reports', 'trade_checks', 'operation_log'] loop
        begin
            execute format('select count(*) from public.%I', t);
            raise exception 'RLS BROKEN: anon can read %', t;
        exception when insufficient_privilege then null;
        end;
    end loop;
    begin
        perform public.delete_my_data();
        raise exception 'PRIVILEGE BROKEN: anon can run delete_my_data()';
    exception when insufficient_privilege then null;
    end;
    if public.username_available('rls_test_a') then raise exception 'SETUP: taken username reported as free'; end if;
    if not public.username_available('rls_free_name') then raise exception 'SETUP: free username reported as taken'; end if;
end $$;

reset role;
rollback;
select 'RLS isolation: all checks passed (nothing was kept)' as result;
