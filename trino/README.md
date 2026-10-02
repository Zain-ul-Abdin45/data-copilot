# Trino, for verifying `COPILOT_DATASOURCE=trino`

A throwaway Trino cluster with one catalog (`warehouse`): a `postgresql` connector pointed at this
project's own Postgres database, through the same read-only `copilot_ro` role the direct-Postgres
path uses (see `catalog/warehouse.properties`). No separate build: Trino reads the `analytics`
schema Postgres already has, so there is no `raw.*` copy step the way DuckDB's verification needs.

```
docker compose up -d                       # trinodb/trino on localhost:8080
```

Then, from `../dbt-test-project` and `../data-copilot` respectively (see the root README's
**Data sources** section for the exact commands): `dbt parse --target trino`, then run the agent
or `evals/oracle.py --engine metricflow` with `COPILOT_DATASOURCE=trino` and the `COPILOT_TRINO_*`
variables set to match this compose file (host `localhost`, port `8080`, catalog `warehouse`,
schema `analytics`).

`docker compose down` when done — this cluster holds no state of its own to preserve.

On Linux, `host.docker.internal` needs the `extra_hosts: host-gateway` line already in
`docker-compose.yml`; Docker Desktop on Mac/Windows resolves it without that, but the line is
harmless there too.
