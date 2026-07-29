# Human correction

This is the complete P8 flow: add a model-origin observation, query it,
append a human assertion at the same effective instant, and query again.

```console
$ .venv/bin/python examples/correction/demo.py
before=blocked origin=model
after=open origin=human
assertions=2
timeline_intervals=1 sources=human-1
```

The original assertion is retained. The answer changes because projection
deterministically ranks the human assertion above the model assertion.
