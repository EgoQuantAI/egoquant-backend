# On-chain sealing: coordination

Between the on-chain agent (branch `onchain`, both repos) and Onur's agents (accounts, Supabase, pages). Answers
go through Savas or Bartu. **Newest entry at the bottom.** Each entry: what is done, what is needed from Onur,
what changed.

The design is `docs/ON_CHAIN_PLAN.md`. Where this file and the plan differ, this file wins (it records Bartu's
later decisions). How it works and its honest limits: `docs/ANCHORING.md` (comes with the code).

**Lanes.** The on-chain agent writes only NEW files: `egoquant/anchoring/**`, `tests/anchoring/**`,
`docs/ANCHORING.md`, this file, `scripts/onchain_new_wallet.py`, and in the frontend `src/lib/proof.ts`,
`src/components/seal-badge.tsx`, `src/app/verify/page.tsx`, and new proxy routes. Onur's side wires them
into `main.py`, `egoquant_routes.py`, the portfolio and account pages and the Supabase migration. The on-chain
agent never edits those files; it asks here.

---

### 2026-10-19 18:00 Dubai: the contract (on-chain agent)

**Done.**
- Branch `onchain` exists off `origin/oct19` in both repos.
- The sealing wallet exists. Its address is public: `0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC`. The key is
  only in Bartu's local backend `.env` (`EGOQ_SEAL_PRIVATE_KEY`). Nobody else needs it until Railway (see 8).
- `scripts/onchain_new_wallet.py`: makes a key, writes it into a git-ignored `.env` (owner-only), prints only
  the address, and refuses to overwrite an existing key.

Everything below is the contract Onur's side builds against. Code and tests follow by Tue 10:00 Dubai.

#### 1. What is sealed, in one paragraph

When a signed-in investor confirms an **approved** saved report, the backend builds a small record of what we
advised (section 2). It writes the record as canonical JSON, draws a random 32-byte salt, and computes
`commitment = SHA-256(salt ‖ record)`. It stores the record and the salt in Supabase (the user's own rows), then
sends a 0-ETH Sepolia transaction from our wallet to itself. The transaction's data is `EGOQ1` plus those
32 bytes, and nothing else goes on-chain. The record includes `versions.report_sha256`, the SHA-256 fingerprint
of the whole saved report JSON, so the seal covers everything the client saw. We **fingerprint** the report. We
never encrypt it, and we never put it on-chain. The seal never changes a number, a limit or a verdict. If the
chain is down, the plan is still confirmed and the seal waits (`pending`).

#### 2. Record fields (`egoq-record-1`)

Built by `egoquant.anchoring.record.build_record(...)` from the saved `reports` row at confirm time.

| Field | From | Note |
|---|---|---|
| `schema` | constant | `"egoq-record-1"` |
| `report_id` | `reports.id` | uuid string |
| `record_version` | 1 | +1 only if a changed report is confirmed again (never today) |
| `confirmed_at` | server clock at confirm | `YYYY-MM-DDTHH:MM:SSZ` |
| `consent_version` | `accounts.routes.CONSENT_VERSION` | the version in the `record_seal` consent row |
| `investor.risk_class` | `report.report.risk_class` | `C1`–`C5` |
| `investor.questionnaire_version` | `report.inputs.answers.questionnaire_version` (else the bank's) | |
| `investor.answers` | **`report.inputs.answers`** (new, see 3) | `{choices, multi_choices, amount_invested_cents, amount_needed_cents}`; money as integer cents |
| `allocation` | `report.report.allocation[]` (`asset_id`, `weight` > 0) | `[{ticker, weight_ppm}]` sorted by ticker; sums to exactly 1 000 000 (largest remainder) |
| `risk_engine.approved` | `report.approved` | must be `true` |
| `risk_engine.violations` | `report.violations` | must be empty |
| `risk_engine.checks` | `report.checks[]` (`name`, `passed`, `detail`) | sorted by name. **Changed from the plan:** the Risk Engine's checks are `{name, passed, detail}`, not `{value, limit, direction, ok}` |
| `versions.engine` | `report.report.engine_version` | Agent A |
| `versions.input_hash` | `report.report.input_hash` | Agent A's input fingerprint |
| `versions.risk_engine` | `egoquant.risk_engine.ENGINE_VERSION` | |
| `versions.risk_profile_config` | SHA-256 of `egoquant/risk_profile/config.yaml` | hex |
| `versions.agent_d_prompt_sha256` | SHA-256 of `egoquant/briefing/prompts/fund_aware_tone.txt` | read only, never edited |
| `versions.agent_d_model` | briefing config `tone.llm_model` | |
| `versions.report_sha256` | **NEW (Bartu):** SHA-256 of the canonical JSON of the saved `reports.report` value, as it is at confirm time | See 2.1 |
| `operation_log` | this report's rows in `operation_log` (`details.report_id` = this id), plus `plan_confirmed` | `[{at, action}]` sorted by `at`, then `action`; `at` is truncated to seconds, UTC |

Never in the record: user id, email, username, IBKR data or tokens, the questionnaire **score**, free text, news
headlines. (The headlines are inside the saved report, which is only *fingerprinted* through `report_sha256`.)

**2.1 Canonical rules.**
- Keys are sorted. The output is compact JSON, UTF-8, with no trailing newline.
- The record itself contains no floats at all. The builder converts to ppm and cents and raises on a float,
  NaN, Infinity or a non-ASCII key.
- For `report_sha256`, the saved report does hold floats, so the same writer runs in "report mode": a number
  with an integral value (`1.0`, `3`) is written as an integer, and any other number as a string with exactly
  six decimals. This gives the same bytes from Python and from the browser.

**2.2 The FINAL report only.** The seal must cover the saved report *after* Agent D's final news briefing was
written (`POST /account/reports/{id}/briefing`, migration 004). Two locks make sure of that:
- the Confirm button is enabled only after that call returned OK, or after the briefing job ended without one
  (section 6);
- migration 006 makes `replace_report_briefing` refuse a report that is already sealed. Without that, a later
  news update would make the saved report differ from its fingerprint.

#### 3. Ask: `POST /account/portfolio` (Onur, `egoquant_routes.py`)

a. **Save the answers with the report.** Before the insert, add the answers to the saved JSON, exactly like this:
```python
saved["inputs"] = {"answers": body.answers.model_dump(mode="json")}
```
This gives `{"questionnaire_version": str|null, "choices": {...}, "multi_choices": {...}, "amount_invested":
float, "amount_needed": float}`. It is the `AnswersIn` the report was really built from. There is no score in
it. The field name is exactly **`inputs.answers`**, at the top level of the saved `report` value, next to
`approved`, `checks` and `report`.

b. **Two log actions, right after the `reports` insert, in this order:**
```python
log_action(token, user, "questionnaire_submitted", {"report_id": out.report_id,
                                                    "questionnaire_version": BANK.questionnaire_version})
log_action(token, user, "report_generated", {"report_id": out.report_id, "approved": out.approved})   # exists
```
Both carry `details.report_id` (the record picks the report's rows by it). `questionnaire_submitted` must be
written after the insert, because before it there is no report id.

c. **Nothing else.** `report_viewed`, `plan_confirmed`, `record_sealed` and `seal_pending` are written by
`egoquant/anchoring/routes.py` (on-chain agent). `POST /account/reports/{id}/viewed` lives there too, so
`egoquant_routes.py` gets no new route.

Test (Onur's side): with the fake store, a saved report has `inputs.answers` equal to the request's answers, and
the log has `questionnaire_submitted` and `report_generated` with the same `report_id`.

#### 4. Ask: consent (Onur, `egoquant/accounts/routes.py`)

- Bump `CONSENT_VERSION` to `"2026-10-20"`.
  **Side effect, please decide:** `has_consent()` matches on `CONSENT_VERSION`, so the bump also invalidates
  existing `ibkr_data` and `trade_check` consents, and users must agree again. If you don't want that, keep
  `CONSENT_VERSION` and add `SEAL_CONSENT_VERSION = "2026-10-20"` instead. Tell me which, and I'll import it.
- No change is needed to `ConsentIn`. The confirm route writes the `record_seal` consent row itself, so that
  agreeing and confirming happen in one click.
- **Consent text** (shown in the Confirm dialog next to an unticked checkbox; also returned by
  `GET /anchoring/status` as `consent_text`, so there is one source):

> **Confirm and seal this plan.** EgoQuant will keep a record of this advice: your risk class, your
> questionnaire answers, the portfolio, the Risk Engine's checks and the steps you took. We publish only a
> fingerprint of it (a 32-byte code with no personal data) on a public test blockchain (Ethereum Sepolia), so
> anyone can later check that the record was not changed. You can download a proof file. A public blockchain
> entry can never be deleted. If you delete your data, we delete the record and its secret key, and the
> fingerprint can then never be linked to you or to anything else. This seals the advice; it places no order
> and moves no money.

#### 5. Ask: migration (Onur reviews, Bideci runs): `supabase/migrations/006_onchain.sql`

**Number:** 006. 003 is the avatar, 004 is the report briefing, and **005 is reserved for Bideci's what-if
hardening**. Neither 005 nor 006 is on any branch yet (checked on all remote branches, 19 Oct 18:00). Run 006
after 005. **If 005 redefines `delete_my_data()` or `purge_expired()`,** the second one written must contain both
sets of tables.

```sql
-- On-chain sealing (egoquant/anchoring). Run once after 001-005. Idempotent.
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
```
(The `delete_my_data` / `purge_expired` bodies are 002_what_if's plus the two new tables. `delete_my_account()`
needs no change, because both tables cascade from `auth.users`.)

**RLS test expectations** (Onur extends `supabase/tests/rls_isolation.sql`):
1. A inserts a record for A's own report: OK. A inserts a record pointing at **B's** report: refused.
2. B selects A's `advisory_records` and `record_seals`: 0 rows. `anon` selects them: permission denied.
3. A inserts a seal row for A's record: OK. A inserts one for B's record: refused.
4. A `update`s or `delete`s their own record or seal row: permission denied (there's no grant).
5. A inserts `created_at` / `expires_at` / `at`: permission denied (column grants).
6. A bad `salt_hex`, a `chain_id` other than 11155111, or an `error_kind` with spaces: refused by the CHECKs.
7. `replace_report_briefing` on A's sealed report raises `report is sealed`; on an unsealed one it still works.
8. After `delete_my_data()`, A has 0 rows in both tables.

**Honest residual risk** (for SECURITY.md): a user can insert extra `record_seals` rows for their own record
through PostgREST, for example a fake "confirmed". That only misleads themselves. `/verify` never trusts
the table: it reads the transaction from the chain. Like `operation_log` (gap 14), the table is an activity
record, and the chain is the proof.

#### 6. Endpoints (new router `egoquant/anchoring/routes.py`)

All owner routes use the accounts helpers (`_signed_in`, `_own`, `_call`, `log_action`, `get_store`), imported
and not edited. Identity comes only from the token. **If `anchor_client_from_env()` is `None`** (sealing not
configured), every route below except `/anchoring/status` answers **503** `{"detail": "Plan sealing is not
available on this server."}`, and the frontend hides the feature.

`SealOut` (used below):
```json
{"status": "pending|submitted|confirmed|failed", "tx_hash": "0x…|null", "chain_id": 11155111,
 "block_number": 1234567, "block_time": "2026-10-20T08:15:42Z", "sealing_address": "0x3c9E…",
 "explorer_url": "https://sepolia.etherscan.io/tx/0x…|null", "error_kind": "rpc_down|null"}
```

| Method, path | Who | Request | Response 200 | Errors |
|---|---|---|---|---|
| `GET /anchoring/status` | public | — | `{"enabled": bool, "chain_id": 11155111, "network": "Sepolia testnet", "sealing_addresses": ["0x…"], "explorer": "https://sepolia.etherscan.io", "consent_version": "2026-10-20", "consent_text": "…"}` (when not enabled: `{"enabled": false}` only) | — |
| `POST /account/reports/{id}/confirm` | owner | `{"consent_version": "2026-10-20"}` (extra fields refused) | `{"confirmed": true, "already_confirmed": bool, "record_id": uuid, "commitment": hex64, "confirmed_at": "…Z", "seal": SealOut}` | 401; 404 not found / not yours; **409 not approved** (nothing stored, nothing sent); 409 consent version changed; 429; 503 |
| `GET /account/reports/{id}/seal` | owner | — | `{"record_id", "commitment", "confirmed_at", "seal": SealOut}`. A `pending` seal is re-sent here (same commitment), and a `submitted` one asks the chain for its receipt. | 404 not confirmed; 503 |
| `GET /account/reports/{id}/proof` | owner | — | proof file, `Content-Disposition: attachment; filename="egoquant-proof-<id>.json"` (see 7) | 404; 503 |
| `POST /account/reports/{id}/viewed` | owner | `{}` | `{"ok": true}`, logs `report_viewed` once per report (repeat calls write nothing) | 404 |
| `GET /verify/tx/{tx_hash}` | public, 20/min per IP | — | `{"found": bool, "chain_id", "from", "to", "sender_ok": bool, "prefix_ok": bool, "commitment": hex64\|null, "status": "confirmed\|pending\|failed\|not_found", "block_number", "block_time"}` | 422 bad hash; 503 |
| `POST /verify` | public, 20/min per IP | the proof file | `{"match": bool, "failed": null\|"hash differs"\|"tx not found"\|"wrong prefix"\|"not sent by EgoQuant"\|"wrong chain"\|"report differs", "checks": {"hash", "tx_found", "chain", "sender", "prefix", "commitment", "report"}, "tx_hash", "block_number", "block_time", "explorer_url"}` (`checks.report` is null when the proof has no report). Stores nothing, logs nothing. | 413; 422 |

Confirm, step by step: load the user's report (404) → 409 unless approved with no violations → if a record
exists, return it (`already_confirmed: true`) → insert the `record_seal` consent + `plan_confirmed` log → build
the record, salt and commitment → insert `advisory_records` → try to seal within **6 s** (PROVISIONAL,
`EGOQ_SEAL_BUDGET_S`) → insert a `record_seals` row (`submitted` + `record_sealed` log, or `pending` +
`seal_pending` log) → answer. The seal never blocks the answer. Caps (over a cap the plan is still confirmed and
the seal stays `pending`, `error_kind: "cap"`): 5 per user per day, 200 per day in total.

#### 7. Proof file (`egoq-proof-1`)

```json
{"proof": "egoq-proof-1", "record": "<canonical record text, exactly>", "salt": "<64 hex>",
 "commitment": "<64 hex>", "chain_id": 11155111, "tx_hash": "0x…", "sealing_address": "0x…",
 "report": { …the saved report JSON, so report_sha256 can be checked too… }}
```
`/verify` (page and API) recomputes `SHA-256(salt ‖ record)`, compares it with `commitment` and with the 32 bytes
after `EGOQ1` in the transaction, and checks the sender against the published sealing addresses. If `report`
is present, it also checks `SHA-256(canonical(report)) == record.versions.report_sha256`. The `/verify` page
does the hashing in the browser and asks the backend only for the transaction, so the answers never leave the
device.

#### 8. Wiring asks, summary (Onur unless noted)

| # | File | Change |
|---|---|---|
| W1 | `requirements.txt` (Umut) | add `eth-account==0.14.0` (installs and signs on Python 3.14; it changed no existing pin in our venv) |
| W2 | `main.py` | `from egoquant.anchoring.routes import router as anchoring_router` + `app.include_router(anchoring_router)`; add `"/verify"` to the 4.5 MB body-limit group (a proof file carries the saved report) |
| W3 | `egoquant_routes.py` | section 3 (answers + two log rows) |
| W4 | `egoquant/accounts/routes.py` | section 4 (consent version) |
| W5 | `supabase/migrations/006_onchain.sql` + `rls_isolation.sql` | section 5 (Bideci runs it) |
| W6 | `src/app/portfolio/page.tsx` (Savas allocates) | the **Confirm this plan** button: shown when `GET /api/anchoring/status` says `enabled`, the user is signed in, `approved` is true, `report_id` is set, **and** the final briefing was saved (`POST /api/account/reports/{id}/briefing` returned OK, or the job ended). Visitors see "Sign in to confirm and seal this plan". A rejected plan shows nothing. The dialog shows `consent_text` with an unticked box; on agree it calls `POST /api/account/reports/{id}/confirm` with `{"consent_version": <status.consent_version>}`, then shows `<SealBadge reportId=… />` and "Download proof file" (`GET /api/account/reports/{id}/proof`). |
| W7 | portfolio / account report view | call `POST /api/account/reports/{id}/viewed` once when a saved report is first shown; the account page may show `<SealBadge>` per report |
| W8 | `docs/SECURITY.md` | a section from `docs/ANCHORING.md` (I'll write the text there) |
| W9 | Railway (Umut) | env names: `EGOQ_SEAL_ENABLED=1`, `EGOQ_SEAL_PRIVATE_KEY` (secret, from Bartu privately), `EGOQ_SEAL_RPC_URLS`, `EGOQ_SEAL_CHAIN_ID=11155111`, `EGOQ_SEAL_EXPLORER=https://sepolia.etherscan.io`, `EGOQ_SEAL_MAX_PER_USER_DAY=5`, `EGOQ_SEAL_MAX_PER_DAY=200` |

The frontend files are mine (new): `src/lib/proof.ts`, `src/components/seal-badge.tsx`, `src/app/verify/page.tsx`,
and new proxy routes `src/app/api/account/reports/[id]/{confirm,seal,proof,viewed}/route.ts`,
`src/app/api/verify/tx/[hash]/route.ts`, `src/app/api/anchoring/status/route.ts`. I won't touch the
`[[...path]]` proxy, because the more specific routes take precedence over it.

**Need from Onur tonight:** OK (or changes) on 3, 4 (which consent option), 5 (the SQL), 6 and W6. Reply
through Bartu or Savas, or append here on your branch.

### 2026-10-19 19:35 Dubai: Bartu's answers to the three open points (on-chain agent)

Bartu's position. Onur has the final say on his own files.

1. **Consent: a separate `SEAL_CONSENT_VERSION = "2026-10-20"`** in `egoquant/accounts/routes.py`.
   **Don't bump `CONSENT_VERSION`**: re-asking every user for IBKR and trade-check consent the night before the
   deadline is too risky. The confirm route writes the `record_seal` consent row with `SEAL_CONSENT_VERSION`,
   and `GET /anchoring/status` returns it as `consent_version`. Until Onur adds the constant, the anchoring code
   falls back to its own `"2026-10-20"` (`egoquant/anchoring/config.py`, `SEAL_CONSENT_FALLBACK`), so nothing
   waits on it.
2. **Freezing sealed reports: yes.** Once a report is sealed, `replace_report_briefing` refuses it. Bartu owns
   that function (Agent D, migration 004) and **approves 006 redefining it** exactly as in section 5 above.
3. **`/verify` in the 4.5 MB body-limit group: yes.** Ask (W2): in `main.py`, `_BODY_LIMITS` first tuple becomes
   `("/portfolio", "/trade-check", "/account", "/debug/portfolio", "/verify")`, next to the `include_router` line.

**Wallet funded:** 0.05 Sepolia ETH on `0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC` (checked on three public RPCs).
---

### 2026-10-19 20:00 Dubai: reply from Onur (accounts, routes, Supabase wiring)

Reviewed the 18:00 contract. **Your three suggestions are all accepted:** (A) a separate `SEAL_CONSENT_VERSION`; (B) sealed reports freeze their news sections (006's `replace_report_briefing` is approved); (C) `/verify` goes into the 4.5 MB body-limit group.

**Already done on `oct19` (backend `db94d92`, tests in `tests/accounts/test_onchain_wiring.py`):**
- W3: every report saved by `/account/portfolio` now holds `inputs.answers` (`questionnaire_version` is never null: the bank's version when the client sent none; no score). The log gets `questionnaire_submitted` and then `report_generated`, both with `details.report_id`. The new log row is **best effort**: until 006 is applied the database refuses the new action name, and a log row must not fail the portfolio.
- W4: `CONSENT_VERSION` stays `"2026-10-19"`; the new constant is `egoquant.accounts.routes.SEAL_CONSENT_VERSION = "2026-10-20"`. `has_consent(token, user, scope, version=None)` takes an optional version. `ConsentIn` stays `ibkr_data | trade_check` on purpose: your confirm route inserts the `record_seal` consent row itself (the insert grant on `consents (user_id, scope, version)` exists).

**006 SQL: approved with two changes.**
1. In `delete_my_data()` add `update public.profiles set avatar = null where user_id = uid;` (a profile picture is personal data and today it survives "delete my data").
2. Numbers: 003 avatar, 004 report briefing, **005 = my what-if hardening** (draft on `oct19`: `docs/supabase/005_what_if_hardening.DRAFT.sql`; Bideci saves it as 005), 006 = yours, run after 005. Bideci's order: 001, 002_hardening, 002_what_if, 003, 004, 005, 006 (runbook: `docs/supabase/FROM_ONUR_runbook.md`).
3. The RLS test list is accepted; Onur extends `supabase/tests/rls_isolation.sql`.

**Wiring still to come (needs your router on this branch, green):**
- W2 `main.py`: `include_router` + `/verify` in the 4.5 MB group. Not before, because importing a missing module would stop the server.
- W1 `requirements.txt` (`eth-account==0.14.0`): please confirm it installs on the Python in `.python-version` (3.14.x). Onur runs `pip-audit` on the dependency tree.
- A briefing save into a sealed report must answer a plain 409 (Onur adds it in `egoquant_routes.py` once your router exists; today the sealed error would surface as a generic one).

**Conditions for the merge into `oct19`:**
1. Everything is OFF unless `EGOQ_SEAL_ENABLED=1` and a key is set; `/anchoring/status` then says `enabled: false` and the frontend hides the feature.
2. Tests use no network and a fake key; no key, RPC URL or RPC error text in any log, response or error.
3. The signer refuses every chain id except 11155111.
4. `/verify` and `/verify/tx`: strict tx-hash regex, 5 s RPC timeout, RPC URLs only from env. "Per IP" cannot see the real IP behind the Vercel proxy, so add a global cap as well.
5. Never push `oct19`; push `onchain`; Onur merges after a full green suite.

**Go / no-go:** code and tests green on `onchain` by **Tue 10:00 Dubai**; Bideci runs 005 and 006 and one live smoke seal by **12:00**. If it is not green by 12:00 the feature ships OFF and the docs say "planned". The core demo must never depend on it.

**For docs/ANCHORING.md (honest limits):** the answers, amounts included, sit in `advisory_records` in clear, owner-only, for 180 days; the chain proves that a record existed unchanged at a time, not that the advice was right; testnet only; FISCO BCOS is future work.

### 2026-10-19 19:45 Dubai (written after Onur's entry stamped 20:00): on-chain agent's reply to Onur

Thanks. Everything in your entry is confirmed, and I have built on it:

- **Merged `origin/oct19` (with `db94d92`) into `onchain`.** The record builder reads exactly your
  `inputs.answers` (`questionnaire_version`, `choices`, `multi_choices`, `amount_invested`, `amount_needed`), uses the
  log rows' `details.report_id`, and the confirm route will use `accounts.routes.SEAL_CONSENT_VERSION`
  (`"2026-10-20"`). The fallback in `anchoring/config.py` is only for an older checkout. A missing
  `questionnaire_submitted` row (best effort, before 006) simply isn't in the record's log, and the confirm still
  works.
- **006, final text with your change:** `docs/anchoring/006_onchain.PROPOSED.sql` on this branch. It is the 18:00
  SQL plus `update public.profiles set avatar = null where user_id = uid;` in `delete_my_data()`. Numbering as
  you wrote: 005 is yours (what-if hardening), 006 is ours. Bideci's order: 001, 002_hardening, 002_what_if, 003,
  004, 005, 006. I checked your 005 draft: it doesn't redefine the bodies of `delete_my_data()` / `purge_expired()`
  (only their grants), so 006's bodies are complete.
- **Your conditions, and where each is met:**
  1. OFF by default: `anchor_client_from_env()` returns `None` unless `EGOQ_SEAL_ENABLED=1`, a valid key **and**
     `EGOQ_SEAL_RPC_URLS` are set. `/anchoring/status` then answers `{"enabled": false}`, and every other anchoring
     route answers 503.
  2. Tests: no network, no real key (a fresh key is made at run time; the RPC node is a fake `requests` session).
     Tests check that the key's hex never appears in a log, a repr, a result or an error. RPC error text is never
     passed on (labels only: `rpc_down`, `rejected`, `nonce`, ...). `tests/anchoring/test_client.py`.
  3. The signer refuses every chain id except 11155111 (`SepoliaAnchorClient` raises; `anchor_client_from_env`
     returns `None`). Tested.
  4. `/verify` and `/verify/tx`: strict hash regex `^0x[0-9a-f]{64}$` (checked before any RPC call), a **5 s total**
     RPC budget per call across all URLs (`client.budget(5)`, tested), RPC URLs only from env on the server, and a
     **global cap** of 120 a minute on top of 20 a minute per IP (PROVISIONAL, `anchoring/config.py`).
  5. I push only `onchain`. Onur merges into `oct19` after a full green suite.
- **W1 `eth-account==0.14.0`:** installs on Python 3.14.0 (our `.venv`), and its wheels covered every dependency.
  It **added** packages (eth-*, hexbytes, rlp, ckzg, pycryptodome, bitarray, cytoolz, toolz, parsimonious, py-ecc)
  and changed **no** existing pin. Over to you for `pip-audit`.
- **Live evidence (step 3 done):** one real Sepolia seal of a FAKE demo record, in block 11949129:
  `0xf3765c5f1ca3ac9836586fa6f69c770942ef73abeae4eaa351fb64c7551df239`
  (https://sepolia.etherscan.io/tx/0xf3765c5f1ca3ac9836586fa6f69c770942ef73abeae4eaa351fb64c7551df239). From = to =
  `0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC`, value 0, input `0x45474f5131` + commitment. CLI verify: "Unchanged ✓".
  The tampered copy (one weight moved by 1 ppm) gives "Does not match ✗ (hash differs)". Both proof files are in
  `docs/anchoring/`.
- **Agreed for the sealed-report 409:** `replace_report_briefing` raises `report is sealed`. Your route maps that
  to a plain 409. The confirm route reads the saved report once, at confirm time.

**Next (by Tue 10:00):** `egoquant/anchoring/routes.py` + tests (step 4), then the frontend files. Then W2
(`main.py`) can go in.

### 2026-10-19 19:53 Dubai: step 4 done, ready to wire (on-chain agent)

**Backend** (branch `onchain`): `egoquant/anchoring/routes.py` implements section 6 exactly. Tests:
`pytest tests/anchoring -m "not live"` covers the full flow, the 409s and 404s, RPC down then recovery, both caps,
`viewed` logged once, 503/hidden when not configured, `/verify` storing and logging nothing, rate limits, and a
real signing client whose key appears in no log, response or store row. The full suite without `tests/regime` is
green: 1 792 passed. How it works and its honest limits are in `docs/ANCHORING.md`, which ends with a suggested
SECURITY.md section (W8).

Two small additions to the contract (additive):
- `POST /verify` failure labels also include `"not yet in a block"` (the transaction exists but isn't mined yet).
- `GET /verify/tx/{hash}` also returns `explorer_url` when found.

**Frontend** (branch `onchain`, new files only; `tsc`, `eslint src` and `next build` are clean):
- `src/lib/proof.ts`: canonical JSON, SHA-256, verification and tamper, identical to the backend.
  `node scripts/check-vectors.mjs` checks 310 shared vectors and the real demo proof. Please consider adding it to
  the build check (`package.json` is not mine).
- Proxies: `src/app/api/account/reports/[id]/{confirm,seal,proof,viewed}`, `src/app/api/verify/tx/[hash]` and
  `src/app/api/anchoring/status`. They take precedence over `[[...path]]`, which I left untouched.
- `src/components/seal-badge.tsx` (`<SealBadge reportId initial? />`) and `src/components/seal-section.tsx`, the
  report's **Seal section**: the Confirm button, the consent dialog (text from `/anchoring/status`), the badge,
  "Download proof file" and a link to /verify.
- `src/app/verify/page.tsx`: public. It loads a proof or the demo proof (`public/demo-proof.json`, a FAKE
  investor), checks it in the browser, and has "Try a tampered copy". Checked locally against the live chain:
  "Unchanged ✓ — block 11,949,129", and the tampered copy says "Does not match ✗ (hash differs)".

**Wiring for Onur (W2, W6, W7):**
1. `main.py`: `from egoquant.anchoring.routes import router as anchoring_router` + `app.include_router(anchoring_router)`,
   and `"/verify"` in the 4.5 MB body-limit tuple.
2. Portfolio page (and optionally each report on the account page), once per saved report:
   ```tsx
   import { SealSection } from "@/components/seal-section";
   <SealSection reportId={result.report_id} approved={result.approved} signedIn={signedIn}
                briefingFinal={/* true once POST /api/account/reports/<id>/briefing returned OK, or the job ended */} />
   ```
   It renders nothing when sealing is off or the plan was rejected, and "Sign in to confirm and seal" for
   visitors.
3. When a saved report is first shown: `fetch(`/api/account/reports/${id}/viewed`, { method: "POST" })`
   (fire and forget).
4. A footer or sidebar link to `/verify` (layout or sidebar is Umut's call).
5. Railway (Umut), names only: `EGOQ_SEAL_ENABLED=1`, `EGOQ_SEAL_PRIVATE_KEY` (secret, from Bartu privately),
   `EGOQ_SEAL_RPC_URLS=https://ethereum-sepolia-rpc.publicnode.com,https://sepolia.drpc.org,https://1rpc.io/sepolia`,
   `EGOQ_SEAL_CHAIN_ID=11155111`, `EGOQ_SEAL_EXPLORER=https://sepolia.etherscan.io`, plus `requirements.txt`
   `eth-account==0.14.0` (W1).

### 2026-10-19 20:02 Dubai: requirements.txt + evidence (on-chain agent)

- **W1 done on `onchain` (Bartu approved, with Umut's authority):** `requirements.txt` gets `eth-account==0.14.0`
  (alphabetical, after `deprecation`). It brings in eth-abi, eth-hash, eth-keyfile, eth-keys, eth-rlp, eth-typing,
  eth-utils, hexbytes, rlp, ckzg, pycryptodome, bitarray, cytoolz, toolz, parsimonious and py-ecc, and changes no
  existing pin. Onur: please include it in your package audit. My own clean-venv `pip-audit` result goes into
  `docs/anchoring/GATE_RESULTS.md`.
- **Evidence:** `docs/ON_CHAIN_EVIDENCE.md` lists 5 real Sepolia seals of fake investors (C3, C1, C5, a 25-fund C4,
  and one confirmed in the browser through the real confirm route). Each is verified, and each tampered copy fails
  with "hash differs".

### 2026-10-19 20:28 Dubai: Gate ready, see docs/anchoring/GATE_RESULTS.md (on-chain agent)

**Gate ready: see `docs/anchoring/GATE_RESULTS.md`.** In short:
- **Merge test:** `onchain` with `oct19` `60fc8e3` merged (backend `7509737`, pushed) gives 2538 passed and 8 failed.
  Plain `oct19` `60fc8e3` gives 2143 passed and the **same 8** failed: 7 `tests/regime/test_regime_goal.py` plus
  `tests/system/test_what_if.py::test_a_scenario_that_breaks_a_cap_is_blocked_without_numbers`. So none are ours. The
  difference of 395 is our tests.
- **Audit:** a clean Python 3.14 venv with the full `requirements.txt` (eth-account included, 78 packages) gives
  `pip-audit 2.10.1`: "No known vulnerabilities found". (This Mac has 3.14.0, not the 3.14.4 in `.python-version`.
  The wheels are the same.)
- **Your conditions:** each one is listed with the test that proves it.
- **Frontend** (with Umut's authority via Bartu, branch `onchain` `c0aa0d2`, merged with `oct19`, pushed): the
  Seal section is on the portfolio Summary for saved reports. The consent dialog is a modal, so it never overlaps the
  report column. `/viewed` is sent once, and "Verify a proof file" is in the sidebar. tsc, eslint and next build
  are clean.
- **Evidence** (`docs/ON_CHAIN_EVIDENCE.md`): 5 seals of **real engine output on real market data** for invented
  personas, one of them confirmed through the portfolio page, plus the earlier hand-written CLI tests, now labelled
  as such. FYI: a C4 `/portfolio` request with 20 interest areas got a **502 after 249 s** on Railway.
- The sealed-report 409 is done on your side (`60fc8e3`). Thanks; our suite passes with it.

**Onur, please:**
1. Review `docs/anchoring/006_onchain.PROPOSED.sql` (the 18:00 SQL plus your `avatar = null` change) so it can be copied
   into `supabase/migrations/006_onchain.sql` and run by Bideci after 005.
2. W2 in `main.py`: `include_router` + `"/verify"` in the 4.5 MB group.
3. Merge `onchain` into `oct19` in both repos when you're satisfied.

### 2026-10-19 21:18 Dubai: /portfolio time budget pushed to oct19 (on-chain agent, for Bartu)

Not on-chain work. Posted here so Onur, Zerya and Damla see it. **`oct19` `80be208`** (an ordinary fast-forward push,
approved by Bartu with Umut's authority):
- **Cause** (measured locally, launcher mode 2 settings, empty cache; Tiingo isn't available locally, so prices came
  from the committed snapshot): Agent D's in-request briefing took **18–62 s**, almost all of it in-request AI
  summaries (sequential OpenAI calls with retries). Agent A (≤0.45 s), the Risk Engine (~0 s) and the what-if
  context (0.02 s) were not the problem. On Railway, Agent C's new Tiingo requests (up to 40 per build, one at a
  time) can add more on top. I couldn't measure that part locally.
- **Fix:** `/portfolio`'s briefing is **cache only** (no feed request, no OpenAI call; the news worker keeps the feed
  cache warm; the briefing job still does the rest), with a hard **5 s** limit. **Agent C** (Zerya/Damla, minimal):
  `fetch_universe(max_seconds=...)` makes no NEW request for an optional fund after **10 s**. Core funds and
  holdings are always priced, cached prices are always used, and a skipped fund says "not fetched in time". At
  most **8 interest areas** (the investor's first) are used for prices and news, and a banner says so in one
  sentence. All three limits are PROVISIONAL constants in `egoquant_routes.py`.
- **Result:** 20 areas, C4: 45 s → 0.6 s. C5: 65 s → 2.6 s (empty cache). Warm cache with news: 2.5 s / 4.6 s.
  Agent A's numbers and the Risk Engine verdict are unchanged: a test checks that 20 areas capped to 8 gives
  exactly the portfolio of asking for those 8.
- **Tests:** `tests/test_portfolio_time_budget.py` and `tests/agent_c/test_fetch_time_limit.py` (9 tests, no network).
  Full suite: 2152 passed, only the known 8 failures.

### 2026-10-19 23:03 Dubai: on-chain MERGED into oct19 and wired (on-chain agent, Bartu's go-ahead with Umut's and Onur's authority)

**Onur's agents: this is done, please don't duplicate it.**
- `onchain` is merged into `oct19` in both repos (ordinary merges).
- `main.py`: includes `egoquant.anchoring.routes.router`, and `/verify` (with `/verify/tx/...`) is in the 4.5 MB
  body-limit group. New test: `tests/anchoring/test_main_wiring.py` (router included, 2 MB proof reaches `/verify`,
  4.6 MB gives 413 unread, off means `{"enabled": false}`/503).
- Onur's sealed-report 409 (`60fc8e3`) is kept unchanged.
- **`supabase/migrations/006_onchain.sql`** (from `docs/anchoring/006_onchain.PROPOSED.sql` with Onur's
  `avatar = null`). Its deletes from `what_if_runs`/`privacy_log` are guarded with `to_regclass`, because the live
  database does NOT have `002_what_if`'s tables yet (read-only probe on 19 Oct; `004`'s function is missing too).
  New `supabase/tests/rls_onchain.sql` covers the 8 RLS expectations. Both files parse with the PostgreSQL parser
  (pglast); they haven't been run against a database yet.
- **Order for the SQL editor:** `002_what_if.sql` → `004_report_briefing.sql` → `005` (when Onur pushes it; it doesn't
  matter before or after 006) → `006_onchain.sql` → `tests/rls_onchain.sql`. **If 002_what_if or 004 is ever run
  again after 006, run 006 again**, because they redefine functions 006 extends.
- Committed proof files (`docs/anchoring/*.proof.json`) were scanned: no key, no `.env` value, no email, token or
  user id. Only demo personas.
- Full suite: 2552 passed, only the known 8 failures.
