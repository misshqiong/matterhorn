# REST service with PostgreSQL

Run the API and a durable PostgreSQL database:

```console
$ docker compose -f examples/service/compose.yml up -d --build
$ .venv/bin/python examples/service/smoke.py
health=ok
matters=0
$ docker compose -f examples/service/compose.yml down
```

Use `down -v` only when you intentionally want to delete the example volume.
The API DSN targets the writable primary directly; do not put a read-replica or
read/write-splitting proxy between Matterhorn and PostgreSQL.
