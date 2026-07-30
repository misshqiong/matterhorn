# Positioning alongside L1 memory tools

Matterhorn complements tools such as mem0, ReMe, and OpenViking; it does not
replace their broad capture, recall, personalization, file organization, or
semantic retrieval.

Those tools are useful upstream sources. Their messages, daily cards,
summaries, or digests can become Matterhorn EpisodeCards when a host can attach
traceable evidence. Matterhorn then adds a narrower contract: closed
predicates, bi-temporal assertions, deterministic projection, source-bearing
answers, replay, and first-class human correction.

This tradeoff is deliberate. Matterhorn is a poor fit for open-ended personal
recollection that benefits from generative interpretation. It is a good fit
when an agent or application must answer “what is currently true?”, “what was
true then?”, and “which evidence supports that answer?” the same way every
time.

## Console versus host product UI

Matterhorn ships a Console operating surface for operators, developers, and
demos. The Console is a static peer client of the same public REST API used by
other clients; it adds no private read path.

Matterhorn still has no built-in business consumption UI. End-user boards,
workflow screens, approvals, notifications, and domain-specific experiences
belong to hosts. The Console exists to inspect, query, correct, feed, and
demonstrate the memory product itself.
