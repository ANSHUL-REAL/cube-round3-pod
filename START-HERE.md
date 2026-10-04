# START HERE

The first document to read after you fork the starter repository. About five minutes.

## What is Round 3?

In Round 2 you built **one agent** that makes one judgment and leaves proof. In Round 3 your Pod of five takes those five existing agents and connects them into **one commerce system**: a unit goes in at supplier delivery, and a traceable final outcome comes out.

**You are not rebuilding your agent.** You are integrating it.

```text
Integrate  →  Orchestrate  →  Test  →  Deploy  →  Demonstrate
```

Round 3 is judged mainly on the **system**: does it work end to end, do the agents interoperate, is the evidence traceable, does it survive failure? Round 2 performance remains part of your final result.

## What are we building?

```text
Receiving
    ↓
Prep
    ↓
Pack
    ↓
Returns
    ↓
Recovery
    ↓
Final Commerce Outcome
```

This is the expected end-to-end commerce journey. Your Pod may design the internal orchestration differently where justified: a unit takes **one** route (FBA goes through Prep, merchant-fulfilled through Pack), and Returns only exists if something came back. What does not change is the common contract every agent speaks, and the fact that one **orchestrator owns the workflow state**.

| Member | Agent | What it does | Folder |
|---|---|---|---|
| 1 | Receiving Manager | Checks what arrived from the supplier against the purchase order: identity, quantity, damage, quality. The only point where a supplier claim is still possible. | `agents/receiving/` |
| 2 | Prep Manager | Checks a unit is prepped correctly for Amazon (polybag, warnings, labels, handling marks) so wrongly charged defect fees can be disputed. FBA units only. | `agents/prep/` |
| 3 | Pack Manager | Checks the open box matches the order before it is sealed: right items, right quantities, nothing extra. Merchant-fulfilled / 3PL units only. | `agents/pack/` |
| 4 | Returns Manager | Checks a returned item: is it what was sold, is it complete, what condition (Amazon's scale), and what to do with it (restock, refurbish, liquidate, dispose). | `agents/returns/` |
| 5 | Recovery Manager | Has no camera. Reads everyone's evidence against the channel's fee reports and decides, charge by charge, whether the evidence contradicts it (claim), supports it, or is silent. | `agents/recovery/` |

(**Specialist Pods** have no Prep Manager; their fifth member is a Specialist / Integration Engineer. See [`FAQ.md`](FAQ.md).)

## What is my job?

> **I own one agent. I bring my Round 2 agent into the Pod. I make it compatible with the common contract. I work with the other four participants. Together we build the orchestration and shared workflow. Every stage produces traceable evidence. The orchestrator owns the workflow state. We test the complete system, handle uncertainty and failures, and demonstrate one end-to-end commerce outcome.**

Concretely:

1. **Bring your Round 2 agent** and put it in `agents/<your-agent>/`.
2. **Make it comply with the common contract** (Agent Input in, Agent Output with an Evidence Record out).
3. **Work with the other four.** Agree the integration architecture together.
4. **Integrate with the Pod orchestrator.** The orchestrator, not you, decides what happens next.
5. **Preserve evidence.** Never invent it, never hide it.
6. **Test the whole workflow**, including UNCERTAIN and failures.
7. **Contribute to the final demo and submission.**

No participant owns the final system alone.

## Try it in two minutes

```sh
make setup      # needs Python 3.11+
make test       # 90+ tests pass on the organiser stubs
make run        # runs 100 sample workflows end to end -> out/
make case UNIT=UNIT-0014 ORG=org_demo_alpha     # one workflow, in full
```

It runs out of the box on **organiser stub agents** that replay the Round 2 sample data. They are not agents: they exist so you can see the whole system work before you plug yours in, and so you can tell whether a failure is yours or the plumbing's. Look at [`examples/`](examples/) to see a happy path, an UNCERTAIN path, a failure path and a full end-to-end run.

## What should I read?

In this order:

1. **`START-HERE.md`** (you are here)
2. [`PARTICIPANT-GUIDE.md`](PARTICIPANT-GUIDE.md): your responsibilities, the Pod model, how to work together
3. [`ARCHITECTURE.md`](ARCHITECTURE.md): the system, what is fixed and what is yours
4. [`INTEGRATION-GUIDE.md`](INTEGRATION-GUIDE.md): how five agents become one system
5. [`EVIDENCE-CONTRACT.md`](EVIDENCE-CONTRACT.md): the shared data shapes
6. [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md): workflow state, statuses, failures, UNCERTAIN
7. [`ROUND3-RUBRIC.md`](ROUND3-RUBRIC.md): how you are scored
8. [`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md): exactly what to hand in

Also: [`GITHUB-GUIDE.md`](GITHUB-GUIDE.md) (how five people share a repo), [`RULES.md`](RULES.md), [`FAQ.md`](FAQ.md), [`DEMO-GUIDE.md`](DEMO-GUIDE.md).
