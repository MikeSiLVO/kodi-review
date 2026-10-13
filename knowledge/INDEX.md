Kodi knowledge: facts about Kodi's code and behavior at the merge base. Where a fact and the code
at head disagree, the code wins; the change may alter what a fact describes.

Each line reads `<kind>: <fact> | check: <what to check> | at: <where in the code>`.
- scenario: an input or setup real users have that code must handle
- invariant: something that must stay true, which a change can quietly break
- external: what a library, protocol, device or platform does that the code must agree with
- trap: current behavior that new code easily gets wrong, the cause of past bugs
- convention: how Kodi does something that new code must match

A `[22]` tag means the fact holds only up to Kodi 22 (Piers), so it applies to Piers code and the
`piers/` copies; `[23+]` means from Kodi 23 (master). On a Piers pull request the `[23+]` lines
are left out.
