# Email matter-tracking demo

This demo feeds a fictional vendor-project mailbox into Matterhorn and exports
one self-contained HTML ledger. The mailbox uses only reserved `.example`
domains. It contains 18 messages; one automated bulk message is deliberately
filtered, leaving 17 traceable Records in two email threads.

The story includes a delivery date moving from June 1 to June 10 to June 20,
an explicit option A to option B reversal, an owner handoff from Mira Venn to
Theo Rill, and an acceptance-memo commitment that remains overdue.

## Run with a configured gateway

`run.sh` performs the full `init → add demo.mbox → flush → export html` chain.
It reads the standard `MATTERHORN_PROVIDER`, `MATTERHORN_BASE_URL`,
`MATTERHORN_MODEL`, and credential environment variables. Missing gateway
configuration fails before any database or output is created.

```console
MATTERHORN_PROVIDER=openai-compatible \
MATTERHORN_BASE_URL=https://gateway.example/v1 \
MATTERHORN_MODEL=example-model \
MATTERHORN_API_KEY=... \
./examples/email/run.sh
```

Set `MATTERHORN_EMAIL_DEMO_DIR` to retain the database and
`MATTERHORN_EMAIL_DEMO_HTML` to choose the output page.

## Offline fixture proof

The checked-in fixture is a deterministic stand-in for extraction; it is not a
claim about real-model output and makes no network request.

```console
MATTERHORN_PROVIDER=fixture \
MATTERHORN_FIXTURE_PATH="$PWD/examples/email/fixture-gateway.json" \
MATTERHORN_MH_BIN="$PWD/.venv/bin/mh" \
./examples/email/run.sh
```

The real distillation run should use the requester's configured gateway after
the fixture-rendered page has been reviewed.
