-- Agent D: a saved report takes the news-briefing JOB's final sections (proposed by Bartu; Onur to review and run).
-- Run once in the Supabase SQL editor after 001-003. Idempotent. Adds one function; changes no table or grant.
--
-- Why
--   /account/portfolio saves the report right away, with a quick, half-rated news briefing. The briefing job then
--   rates every headline (the News tab shows that version). The saved report must hold the same final version,
--   because the on-chain seal fingerprints the saved report.
--   `reports` has no UPDATE grant (by design: the user may not rewrite Agent A's or the Risk Engine's numbers), so
--   instead of a grant this function replaces ONLY Agent D's six keys inside report -> 'report', for the caller's
--   own row. Every other key, and every number in it, stays as /portfolio saved it.
--
-- Security
--   * security definer, but the row must belong to auth.uid() (anonymous callers are refused).
--   * only the six Agent D keys are accepted; anything else raises.
--   * the backend checks first that the briefing job belongs to this report (a digest of its numbers, sealed at
--     /portfolio time). A user calling this function directly can only change the news sections of their own
--     report, never its portfolio numbers, and never anyone else's report.

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
    update public.reports
       set report = jsonb_set(report, '{report}', coalesce(report -> 'report', '{}'::jsonb) || sections)
     where id = rid and user_id = uid;
    if not found then raise exception 'report not found'; end if;
end $$;

revoke all on function public.replace_report_briefing(uuid, jsonb) from public, anon, authenticated;
grant execute on function public.replace_report_briefing(uuid, jsonb) to authenticated;
