# Build log

Keep this current. Organisers read it, and it is evidence of how the Pod actually worked. One entry per working session; newest first. Be honest about what failed.

| Date (UTC) | Who | What we did | What we learned / what broke | Next |
|---|---|---|---|---|
| 2026-10-07 | @ANSHUL-REAL | Put the Round 2 Pack Manager into agents/pack/ (core copied unchanged, adapter, 18 tests, README, PROVENANCE). Branch feature/pack on top of fix/posix-input-refs | Starter fails 2 tests on Windows before any change: backslashes in capture refs (fixed) and a 1 s dead-agent timeout that Windows needs 2 s to refuse (left alone). Real Gemini not yet run through this repo | Live run with a real key and box photo; agree the Prep owner; confirm pod type |
| 2026-10-07 | @ANSHUL-REAL | Hardened Pack: persistent photo-reuse ledger, model time budget inside the 30 s stage timeout, `agents.pack.check` CLI, whole-workflow test with Returns and Recovery receiving Pack's record (24 Pack tests) | Round 2's 25 s x 2 model timeout could outlive the orchestrator's timeout; my first CLI test had an assertion that could never fail, caught and replaced | Live Gemini run with a real key and a box photo; teammates' stages |
