-- EgoQuant seal v2 (egoquant/anchoring/sealed.py). Run AFTER 006_onchain.sql (and 007). Idempotent. Adds one column, no
-- table, no policy, and does not touch delete_my_data() or purge_expired(), so 007 does not need to run again.
--
-- The SEALED DOCUMENT: the final client report rendered once at confirm, stored byte-exact next to its record. It is
-- owner-only through advisory_records' row-level security (006), has no update grant (insert-only, like the record), and
-- is erased with the record by delete_my_data() / purge_expired() (007) and by cascade from the report and the account.
-- Record schema 2 adds the document's fingerprint and the username it was sealed for. Skipped when 006 was not run.
do $$
begin
    if to_regclass('public.advisory_records') is not null then
        alter table public.advisory_records add column if not exists sealed_document text;
        alter table public.advisory_records drop constraint if exists advisory_records_sealed_document_check;
        alter table public.advisory_records add constraint advisory_records_sealed_document_check
            check (sealed_document is null or length(sealed_document) < 8000000);
        alter table public.advisory_records drop constraint if exists advisory_records_schema_check;
        alter table public.advisory_records add constraint advisory_records_schema_check
            check (schema in ('egoq-record-1', 'egoq-record-2'));
        grant insert (user_id, report_id, record_version, schema, record_canonical, salt_hex, commitment_hex,
                      report_sha256, sealed_document) on public.advisory_records to authenticated;
    end if;
end $$;
-- Check: select column_name from information_schema.columns where table_name = 'advisory_records' and column_name = 'sealed_document';
