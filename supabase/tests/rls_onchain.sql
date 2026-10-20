-- Row-Level Security proof for on-chain sealing (006_onchain.sql). Run in the Supabase SQL editor AFTER 006.
-- One transaction, ROLLED BACK at the end: no test user or row is left behind. Any failure raises an exception naming
-- the broken rule; success ends with one row "RLS on-chain: all checks passed".

begin;

insert into auth.users (id, email, raw_user_meta_data, aud, role)
values ('00000000-0000-0000-0000-0000000000ca', 'rls-oa@example.invalid', '{"username": "rls_onchain_a"}', 'authenticated', 'authenticated'),
       ('00000000-0000-0000-0000-0000000000cb', 'rls-ob@example.invalid', '{"username": "rls_onchain_b"}', 'authenticated', 'authenticated');

-- Rows with fixed ids are made by the table owner (signed-in users may not choose an id: 002/006 column grants), then the
-- test acts as each user. B owns one report and its record (A will try to point at them); A owns two reports and a record.
insert into public.reports (id, user_id, report) values
    ('00000000-0000-4000-8000-0000000000b1', '00000000-0000-0000-0000-0000000000cb', '{"test": true}'),
    ('00000000-0000-4000-8000-0000000000a1', '00000000-0000-0000-0000-0000000000ca', '{"test": true}'),
    ('00000000-0000-4000-8000-0000000000a3', '00000000-0000-0000-0000-0000000000ca', '{"report": {}}');
insert into public.advisory_records (id, user_id, report_id, record_version, schema, record_canonical, salt_hex, commitment_hex, report_sha256)
values ('00000000-0000-4000-8000-0000000000b2', '00000000-0000-0000-0000-0000000000cb', '00000000-0000-4000-8000-0000000000b1', 1,
        'egoq-record-1', '{}', repeat('b', 64), repeat('2', 64), repeat('c', 64)),
       ('00000000-0000-4000-8000-0000000000a2', '00000000-0000-0000-0000-0000000000ca', '00000000-0000-4000-8000-0000000000a1', 1,
        'egoq-record-1', '{}', repeat('a', 64), repeat('1', 64), repeat('d', 64));

-- A: own seal, log line and consent, through the user's own grants.
set local role authenticated;
select set_config('request.jwt.claims', '{"sub": "00000000-0000-0000-0000-0000000000ca", "role": "authenticated"}', true);
insert into public.record_seals (record_id, status, chain_id, tx_hash)
values ('00000000-0000-4000-8000-0000000000a2', 'submitted', 11155111, '0x' || repeat('e', 64));
insert into public.operation_log (action, details) values ('plan_confirmed', '{"report_id": "x"}');
insert into public.consents (scope, version) values ('record_seal', 'test');

do $$
begin
    -- 1. A sees only their own record and seal.
    if (select count(*) from public.advisory_records) <> 1 then raise exception 'RLS BROKEN: A sees B''s record'; end if;
    if (select count(*) from public.record_seals) <> 1 then raise exception 'SETUP: A should see 1 seal'; end if;

    -- 2. A cannot point a record at B's report.
    begin
        insert into public.advisory_records (report_id, record_version, schema, record_canonical, salt_hex, commitment_hex, report_sha256)
        values ('00000000-0000-4000-8000-0000000000b1', 2, 'egoq-record-1', '{}', repeat('a', 64), repeat('3', 64), repeat('d', 64));
        raise exception 'RLS BROKEN: A created a record for B''s report';
    exception when insufficient_privilege or check_violation then null;
    end;

    -- 3. A cannot add a seal row to B's record, even knowing its id.
    begin
        insert into public.record_seals (record_id, status, chain_id)
        values ('00000000-0000-4000-8000-0000000000b2', 'confirmed', 11155111);
        raise exception 'RLS BROKEN: A added a seal to B''s record';
    exception when insufficient_privilege or check_violation then null;
    end;

    -- 4. Insert-only: no update, no delete of records or seals.
    begin
        update public.advisory_records set record_canonical = '{"changed": true}';
        raise exception 'PRIVILEGE BROKEN: A updated a sealed record';
    exception when insufficient_privilege then null;
    end;
    begin
        update public.record_seals set status = 'confirmed';
        raise exception 'PRIVILEGE BROKEN: A updated a seal row';
    exception when insufficient_privilege then null;
    end;
    begin
        delete from public.record_seals;
        raise exception 'PRIVILEGE BROKEN: A deleted a seal row';
    exception when insufficient_privilege then null;
    end;

    -- 5. No timestamps or expiry chosen by the user.
    begin
        insert into public.record_seals (record_id, status, chain_id, expires_at)
        values ('00000000-0000-4000-8000-0000000000a2', 'pending', 11155111, now() + interval '10 years');
        raise exception 'PRIVILEGE BROKEN: A set expires_at on a seal';
    exception when insufficient_privilege then null;
    end;

    -- 6. The CHECKs: bad salt, another chain, an error_kind that is a message.
    begin
        insert into public.advisory_records (report_id, record_version, schema, record_canonical, salt_hex, commitment_hex, report_sha256)
        values ('00000000-0000-4000-8000-0000000000a1', 3, 'egoq-record-1', '{}', 'nothex', repeat('4', 64), repeat('d', 64));
        raise exception 'CHECK BROKEN: a bad salt was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into public.record_seals (record_id, status, chain_id) values ('00000000-0000-4000-8000-0000000000a2', 'pending', 1);
        raise exception 'CHECK BROKEN: mainnet chain id accepted';
    exception when check_violation then null;
    end;
    begin
        insert into public.record_seals (record_id, status, chain_id, error_kind)
        values ('00000000-0000-4000-8000-0000000000a2', 'pending', 11155111, 'RPC said: boom');
        raise exception 'CHECK BROKEN: an error message was stored as error_kind';
    exception when check_violation then null;
    end;

    -- 7. A sealed report's news sections are frozen; an unsealed one still takes them.
    begin
        perform public.replace_report_briefing('00000000-0000-4000-8000-0000000000a1', '{"what_changed": []}'::jsonb);
        raise exception 'FREEZE BROKEN: a sealed report took new news sections';
    exception when raise_exception then
        if sqlerrm <> 'report is sealed' then raise; end if;
    end;
    perform public.replace_report_briefing('00000000-0000-4000-8000-0000000000a3', '{"what_changed": []}'::jsonb);

    -- 8. Delete my data removes the records and seals.
    perform public.delete_my_data();
    if (select count(*) from public.advisory_records) <> 0 or (select count(*) from public.record_seals) <> 0 then
        raise exception 'DELETE BROKEN: records or seals survived delete_my_data()';
    end if;
end $$;

-- B's record is untouched by A's delete_my_data().
select set_config('request.jwt.claims', '{"sub": "00000000-0000-0000-0000-0000000000cb", "role": "authenticated"}', true);
do $$
begin
    if (select count(*) from public.advisory_records) <> 1 then raise exception 'RLS BROKEN: A''s delete removed B''s record'; end if;
end $$;

-- Anonymous: nothing.
reset role;
set local role anon;
do $$
declare t text;
begin
    foreach t in array array['advisory_records', 'record_seals'] loop
        begin
            execute format('select count(*) from public.%I', t);
            raise exception 'RLS BROKEN: anon can read %', t;
        exception when insufficient_privilege then null;
        end;
    end loop;
end $$;

reset role;
rollback;
select 'RLS on-chain: all checks passed (nothing was kept)' as result;
