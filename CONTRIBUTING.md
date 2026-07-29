# Contributing to Matterhorn

Thank you for helping build a verifiable memory layer.

## Specification first

`spec/SPEC.md` is the source of truth shared by independent implementations.
Any change to normative behavior **must include a corresponding change to the
language-neutral conformance cases**. A specification change without a golden
case change is incomplete and must not be merged.

When behavior and implementation disagree:

1. decide the language-neutral behavior in the specification;
2. add or update a golden YAML case;
3. make every implementation pass that case; and
4. add focused implementation tests only where they improve diagnostics.

Golden YAML must not contain Python, Java, SQL, or executable expressions.

## Local checks

Use Python 3.11 or newer in a repository venv:

```console
$ python3.12 -m venv .venv
$ .venv/bin/pip install -e '.[api,mcp,postgres,dev]'
$ .venv/bin/python -m pytest -q
$ .venv/bin/mh conformance run
```

Run both stores with the disposable service:

```console
$ docker compose -f compose.postgres.yml up --build --abort-on-container-exit \
    --exit-code-from conformance
```

Keep the engine schema-agnostic, keep distillation out of reads, preserve
assertion immutability, and add source evidence to every test input.
