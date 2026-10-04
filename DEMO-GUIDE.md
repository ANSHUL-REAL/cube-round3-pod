# Demo Guide

The demo is where the system either exists or it does not. It must be of the **submitted commit, running**, and it should **prove the system works, not simply present slides**.

**Recommended length: about 10 minutes plus questions** (the organisers confirm the slot, **TBA**). Rehearse twice with a timer.

## Recommended structure

| Time | Segment | Show |
|---|---|---|
| 0:00 - 1:00 | **1. Problem** | The commerce chain in one sentence; the Pod; Standard or Specialist. |
| 1:00 - 2:30 | **2. Architecture** | One diagram: the five agents and **the orchestrator**; who owns workflow state; the two or three design decisions that matter (from `docs/decisions.md`). |
| 2:30 - 5:00 | **3. End-to-end execution** | Run **one complete workflow** live, from case to Final Outcome. Say which agents are real and which are stubs. |
| 5:00 - 6:00 | **4. Evidence** | How evidence moves between stages: open a record, show `previous_evidence` / `upstream_refs`, walk the Final Outcome back to a check and the input's hash. |
| 6:00 - 8:00 | **5. Exception** | An **UNCERTAIN** (preserved, `BLOCKED`, resolved by an override that references the original) **and** a **failure** (kill an agent: recorded error, `FAILED`/`INCOMPLETE`, then `resume`). |
| 8:00 - 9:00 | **6. Final decision** | How the system reached its Final Outcome: the rules, `effective_verdicts`, a claim with its evidence and a SILENT charge left alone. |
| 9:00 - 10:00 | **7. Engineering** | Briefly: repo, tests and CI, deployment, numbers and limits, what you would do next. |

The three workflows to use: a **clean** one (30 seconds), an **UNCERTAIN/exception** one, and a **claim** one.

## What the demo must show

| # | Must show | Criterion |
|---|---|---|
| 1 | A unit going in at Receiving and a Final Outcome coming out | 1 |
| 2 | The five agents working together, each handing evidence on through the orchestrator | 1, 2 |
| 3 | The important decisions: routing, orchestration, final-outcome logic, and why | 3, 4 |
| 4 | The evidence flow, back to the exact input | 5 |
| 5 | An UNCERTAIN preserved and resolved by an override | 4, 5 |
| 6 | A failure handled and recorded, never reported as success | 6 |
| 7 | The final output a seller/reviewer actually receives | 7 |

Also briefly: tenancy (the second org sees nothing), your evaluation numbers (false positives and negatives separately), and what does not work.

## Who speaks

Every member speaks to **their own agent and decisions**. The Pod explains the orchestrator together: everyone should be able to. One person talking for ten minutes while four watch scores badly on *Team collaboration*.

## Do

- Run the real system from the tagged commit. Say which agents are stubs.
- Show verdicts, confidence, UNCERTAIN and status in a UI or readable view, not raw JSON.
- Show one thing going wrong and the system coping.
- Be honest about limits.
- Have a **recorded fallback** in case the live run breaks, and say so if you use it.

## Don't

- Don't demo a mock-up, a screenshot of a UI, or hand-edited JSON.
- Don't pick only passing units. Don't hide UNCERTAIN.
- Don't claim "tamper-proof", "immutable" or "production-ready" unless you can show it.
- Don't spend half the time on slides or the problem statement.
- **Don't expose credentials**: check your terminal, `.env` and browser tabs before sharing your screen.

## Pre-demo checklist

```text
[ ] Running from the tagged commit, where we will present
[ ] Three workflows chosen and verified the same day
[ ] A failure we can trigger in one command, and how to bring the agent back
[ ] An UNCERTAIN case and the override command ready
[ ] Tenancy check ready (second org)
[ ] The evidence trail opens in under two steps
[ ] Numbers match the evaluation document
[ ] Recorded fallback saved and its link works
[ ] Everyone knows their part; timed twice
[ ] No credentials visible; screen sharing, audio and API quotas checked
```

## A recorded demo

A single continuous take of the real system; screen and voice; the same structure; no cuts across the failure demo; a link that opens in a private window. Put it in your submission and your `README.md`.
