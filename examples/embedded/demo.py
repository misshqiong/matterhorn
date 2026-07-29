from __future__ import annotations

import tempfile
from pathlib import Path

from matterhorn import Engine


with tempfile.TemporaryDirectory(prefix="matterhorn-embedded-") as directory:
    engine = Engine(Path(directory) / "memory.db", "org-matters/v1")
    assertions = engine.ingest(
        [
            {
                "card_id": "embedded-1",
                "scope_id": "demo",
                "subject_key": "release",
                "date": "2026-07-29",
                "title": "Matterhorn release",
                "status": "open",
                "next_step": "Run conformance",
                "source_refs": [
                    {
                        "source_id": "msg-1",
                        "sent_at": "2026-07-29T09:00:00Z",
                        "sender": "ada",
                    }
                ],
            }
        ]
    )
    status = engine.query.current("demo", "release", "status")[0]
    next_step = engine.query.current("demo", "release", "next_step")[0]
    print(f"assertions={len(assertions)}")
    print(f"status={status.value} sources={','.join(status.source_ids)}")
    print(f"next_step={next_step.value}")
