from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from matterhorn import Engine


clock = iter(
    [
        datetime(2026, 7, 29, 9, 5, tzinfo=timezone.utc),
        datetime(2026, 7, 29, 9, 10, tzinfo=timezone.utc),
    ]
)
with tempfile.TemporaryDirectory(prefix="matterhorn-correction-") as directory:
    engine = Engine(
        Path(directory) / "memory.db",
        "org-matters/v1",
        clock=lambda: next(clock),
    )
    engine.ingest(
        [
            {
                "card_id": "correction-1",
                "scope_id": "demo",
                "subject_key": "release",
                "date": "2026-07-29",
                "occurred_at": "2026-07-29T09:00:00Z",
                "title": "Matterhorn release",
                "status": "blocked",
                "source_refs": [
                    {
                        "source_id": "msg-1",
                        "sent_at": "2026-07-29T09:00:00Z",
                        "sender": "bot",
                        "excerpt": "The release appears blocked.",
                    }
                ],
            }
        ]
    )
    before = engine.query.current("demo", "release", "status")[0]
    print(f"before={before.value} origin={before.origin}")

    engine.correct(
        {
            "scope_id": "demo",
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "status",
            "object_value": "open",
            "valid_from": "2026-07-29T09:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-1",
                    "sent_at": "2026-07-29T09:08:00Z",
                    "sender": "ada",
                    "excerpt": "I checked the gate; the release is open.",
                }
            ],
        }
    )
    after = engine.query.current("demo", "release", "status")[0]
    timeline = engine.query.timeline("demo", "release", "status")
    print(f"after={after.value} origin={after.origin}")
    print(f"assertions={len(engine.store.assertions('demo'))}")
    print(f"timeline_intervals={len(timeline)} sources={','.join(after.source_ids)}")
