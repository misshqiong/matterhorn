# Embedded SQLite

Run from the repository root:

```console
$ .venv/bin/python examples/embedded/demo.py
assertions=2
status=open sources=msg-1
next_step=Run conformance
```

The example creates a temporary SQLite database, queues one evidence-backed
card, flushes it with an empty offline semantic fixture, and reads two
projected values without an LLM on the read path.
