-- One-time database setup. Run as a superuser or the database owner:
--     createdb data_copilot
--     psql -d data_copilot -f setup_db.sql
--
-- The agent connects as copilot_ro, which can read the analytics schema and
-- nothing else. Safety comes from these grants, not from the prompt.

create schema if not exists raw;        -- source tables, loaded by seed_db.py
create schema if not exists analytics;  -- dbt models and seeds; the only schema the agent sees

do $$ begin
  if not exists (select from pg_roles where rolname = 'copilot_ro') then
    create role copilot_ro login password 'copilot_ro';   -- change outside local development
  end if;
end $$;

grant connect on database data_copilot to copilot_ro;
alter role copilot_ro set statement_timeout = '15s';
alter role copilot_ro set default_transaction_read_only = on;

grant usage on schema analytics to copilot_ro;
grant select on all tables in schema analytics to copilot_ro;
-- tables dbt creates later (it drops and recreates them on every run)
alter default privileges in schema analytics grant select on tables to copilot_ro;
