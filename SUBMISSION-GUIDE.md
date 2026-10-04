# Submission Guide

Exactly what a Pod submits, and how. **One submission per Pod.**

## Deadline, time zone and finality

| | |
|---|---|
| **Final submission deadline** | **TBA**, announced by the organisers |
| **Time zone** | **IST (UTC+5:30)**, as in Round 2 (the organisers will confirm) |
| **Late submissions** | **Not accepted.** Carried over from Round 2: the form closes permanently at the deadline |
| **Resubmission** | **Not allowed.** Carried over from Round 2: no reopening, no resubmission |
| **Which submission is final** | The one you submit through the official form. What is assessed is the commit named by your tag `round3-final` (and the links in the form); anything pushed after the deadline is ignored |

These finality rules are carried over from Round 2 and apply unless the organisers say otherwise. Check everything before you submit. See [`RULES.md`](RULES.md) section 9.

## The checklist

```text
[ ] Final GitHub repository
[ ] Five agents integrated            (Specialist Pods: the four agents in your flow)
[ ] Orchestrator working
[ ] Shared contracts implemented
[ ] Evidence traceability working
[ ] Integration tests passing
[ ] End-to-end test passing
[ ] README complete
[ ] Architecture documented
[ ] Decisions documented
[ ] Demo ready
[ ] Deployment URL provided if applicable
[ ] Final submission link submitted
```

And the items the checklist implies:

```text
[ ] LinkedIn post published, tagging CodeQuesters and Sydon.AI; URL ready for the form   (mandatory in Round 2; the organisers confirm for Round 3; template TBA)
[ ] Evaluation and testing evidence (see below)
[ ] Tag round3-final on the submitted commit, pushed
[ ] No secrets in the repo or its history; .env.example complete
[ ] pod.json complete: every member, their GitHub handle, their agent
[ ] Every link opens in a private window, without your personal accounts
[ ] Every Pod member has read the final submission and agrees
```

## What you submit

| # | Item | Requirement |
|---|---|---|
| 1 | **Final GitHub repository** | Your Pod's fork of the starter, tagged `round3-final`, CI green, readable by the organisers (see `GITHUB-GUIDE.md`). |
| 2 | **A working integrated system** | `make setup && make test && make run` (or your documented equivalent) on a clean clone, with the Pod's real agents, not the stubs, except where you say so in `agent.json` and the demo. |
| 3 | **README and documentation** | `README.md` accurate for *your* Pod; `docs/decisions.md` and `docs/build-log.md` current. |
| 4 | **Architecture** | `ARCHITECTURE.md` describing **your** system: components, orchestrator and workflow state, final-outcome logic, tenancy, failure model, deployment. |
| 5 | **Demo / presentation** | Per [`DEMO-GUIDE.md`](DEMO-GUIDE.md): a recorded video link and/or a live slot, and slides if used. |
| 6 | **Deployment URL**, if applicable | Reachable without your accounts, with a note on how to try it. If you did not deploy, say so and give the one-command local run. |
| 7 | **Evaluation and testing evidence** | The numbers and the method (below), in `docs/evaluation.md`. |
| 8 | **LinkedIn post URL** | Carried over from Round 2 (see above). |
| 9 | **Pod details** | `pod.json`. |
| 10 | **Final submission link** | The official form (TBA). |

## "Working integrated system" means

- `agent.json` says what each agent really is; a stub left in the flow is labelled, in the demo too.
- **Your own units** (not only the Round 2 sample) go through it, with real captures in `data/input/`.
- At least one **clean**, one **exception**, one **claim**, and one **UNCERTAIN** that reaches a human-visible outcome (`BLOCKED`/`NEEDS_REVIEW`), then is resolved by an override.
- Killing an agent gives a **recorded error** and a `FAILED`/`INCOMPLETE` workflow: not a crash, not a success.
- A wrong-tenant request is refused.

## Your evaluation document (`docs/evaluation.md`)

Short, numeric and honest:

```text
Method        how many units (recommended: at least 50 UNSEEN, held out from tuning), how chosen,
              who labelled them (two independent humans), how labels were compared, human agreement
Per check     TP / TN / FP / FN (false positives and negatives SEPARATELY) and UNCERTAIN count, for each check
System        end-to-end: final outcomes and statuses by type; share needing a human; FAILED rate under failure injection
Claims        Recovery: PRECISION of recommended claims first (a wrong claim costs standing)
Cost          model calls and USD per unit, per stage; latency
Failure modes the named ways it breaks, with examples
Limits        what you did not test; what the sample data cannot tell you
```

Do not report "accuracy" alone. Do not drop UNCERTAIN cases from the denominator.

## Process

1. Tag the final commit: `git tag round3-final && git push origin round3-final`.
2. **Everyone** does a fresh clone of the tag in a new directory and runs the checklist.
3. One member submits through the **official Round 3 submission form (TBA)** with: repo URL and tag, demo link, deployment URL (if any), the evaluation document path, the LinkedIn post URL, and any other fields the form asks for.
4. Save the confirmation and tell your Pod.
