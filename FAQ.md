# FAQ

**Do we rebuild our Round 2 agent?**
No. The objective is to integrate and adapt the existing Round 2 agent.

**Can we change our Round 2 agent?**
Yes, where required for integration, reliability, compatibility or improvement, while preserving its intended functionality. Keep your Round 2 repository as submitted; make the changes in the Pod repo and record the origin in `PROVENANCE.md`.

**Are all five agents mandatory?**
Yes, unless the official competition rules explicitly say otherwise. They do for **Specialist Pods**: the Pods that have no Prep Manager run four agents (Receiving, Pack, Returns, Recovery) plus a Specialist / Integration Engineer. The organisers will tell you if you are a Specialist Pod; then set `pod_type` and `flow` in `pod.json` to use `orchestration/flow.specialist.json`. Without Prep evidence, Recovery must treat inbound-defect charges as SILENT, not guess. Nobody else may drop an agent.

**Do we need an orchestrator?**
Yes. The Pod needs a mechanism responsible for coordinating the overall workflow and **maintaining workflow state**. A starter orchestrator is provided; extend it or replace it, but it must still own the state, record evidence, and handle failures and UNCERTAIN (see [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md)).

**Can we use LangGraph / LangChain / custom Python / REST / queues?**
Technology choices are open unless the official rules restrict them. The common **logical contracts are mandatory** (Agent Input, Agent Output, Evidence Record, Workflow State, Final Outcome); the implementation technology is yours. Any language works for an agent if it serves `GET /health` and `POST /run`.

**Can agents communicate directly?**
The Pod may choose its internal communication architecture, but the orchestrator must remain responsible for overall workflow state and traceability. If agents talk to each other, the exchange must still be recorded as evidence and visible in the workflow state. The starter design routes everything through the orchestrator.

**What happens when an agent returns UNCERTAIN?**
The result must be preserved as evidence. It must **not** silently become PASS or FAIL. The orchestrator determines the next action according to the Pod's defined policy: by default it continues, marks the workflow `BLOCKED` with outcome `NEEDS_REVIEW`, and waits for a person, whose decision is recorded as an **override** (actor, reason, timestamp, referencing the original). See [`examples/uncertain-path/`](examples/uncertain-path/).

**Can we skip a stage?**
Only if the architecture and competition rules explicitly permit it. A stage is skipped only when the flow's routing rule says it does not apply (for example Prep for a merchant-fulfilled unit), and the **reason is recorded**. It must never be skipped because an agent was unavailable or inconvenient: that is an error, and the workflow ends `FAILED`/`INCOMPLETE`, not "success".

**Can we add additional services or components?**
Yes, where allowed, provided the required workflow and contracts remain demonstrable. Say what you added in `ARCHITECTURE.md`.

**Who owns the final system?**
The Pod, jointly. No participant owns it alone. Each member owns one agent; the orchestration, contracts, integration, tests, documentation and demo are shared.

**Who owns the workflow state?**
The orchestrator. Agent outputs are evidence used to inform state transitions; agents never write workflow state.

**What if two documents disagree?**
That is a **finding**, not a failure. State your assumption in `docs/decisions.md` and open a `finding` issue. Several Round 2 contradictions are already recorded there.

**Do we need a LinkedIn post?**
It was mandatory in Round 2 (tagging CodeQuesters and Sydon.AI). It is carried over to Round 3 unless the organisers say otherwise. See [`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md).

**What is actually graded?**
The integrated Pod system: end-to-end integration, agent interoperability, orchestration, decision quality, evidence and traceability, reliability and error handling, UX and demo, engineering quality, and collaboration. Five agents working independently is not the same as a successfully integrated Pod system. See [`ROUND3-RUBRIC.md`](ROUND3-RUBRIC.md).

**Is it OK if the stubs pass all the tests?**
That proves the plumbing, not your agent. The honest question for your demo is whether *your* agents, on real captures, produce evidence the next agent can use.
