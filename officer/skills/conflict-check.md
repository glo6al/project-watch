Name: Conflict check
Use when: The owner asks whether agents overlap, touch the same files, or may conflict with one another.
Tools: room_snapshot, evidence_query, evidence_expand

1. Establish the agents, project, and period using stable identities from room_snapshot, including qualified branch observations when available. Query scoped file_ref and operation records with evidence_query, and inspect surrounding context/results with evidence_expand. Duplicate filenames in distinct projects are distinct observations.
2. Compare exact recorded paths within their project context. A shared project or branch alone is not a shared file. Keep reads, writes, edits, attempted changes, and agent statements distinct. A reviewer reading a file is not another editor changing it.
3. Report the observed overlap: who touched which file, in what way, and in which period. Retain probably, failed, no result yet, and outcome not recorded. A process command that mentions a file does not prove a recorded file change.
4. Shared file touches are a reason to inspect the work; they do not prove a merge conflict, overwritten changes, broken behavior, or incompatible edits. Only report a concrete conflict if the evidence records it. Otherwise say "I can't tell from the log" whether the changes conflict.
5. Mention the relevant search and coverage limits. An empty or bounded search does not prove there is no overlap elsewhere. Retained memory has independent identities and partial coverage; inferred relations do not establish delegation.
6. Give a concise comparison and any recorded unresolved concern. Keep source citations attached to the actual entries consulted.

This is an evidence check. It does not authorize edits, approvals, messages, or controls, and must not claim that work was coordinated or stopped.
