# Rules

Round 3 has the same shape as Round 2's rules: **repository rules** (how you manage the work) and **engineering rules** (part of what you are assessed on). The difference is that this round is a **Pod build**, so there are rules about who is responsible for what.

> Items marked **TBA** will be set by the organisers and announced through the official Buildathon channels. Anything marked "carried over" is a Round 2 rule that stands unless the organisers say otherwise.

## 1. Individual vs Pod responsibilities

| | Individual (you) | Pod (all five together) |
|---|---|---|
| **Agent** | Your agent in `agents/<name>/`: its adapter, its contract compliance, its honesty about what it does | |
| **Evidence** | Your agent's records are valid, truthful and traceable | The end-to-end evidence chain holds: every final outcome traces to inputs |
| **Integration** | Your agent passes the contract tests and fails open | The orchestrator and workflow state, routing, UNCERTAIN handling, final decision, error handling |
| **Repo** | Your commits, under your own name; reviewing teammates' PRs | `main` stays runnable; shared-file changes discussed as a Pod; the final tagged commit; accurate README/ARCHITECTURE |
| **Demo** | You can explain and defend your agent and its decisions | One coherent demo of one system |
| **Scoring** | Round 2 result stays part of your final result; your individual contribution is visible in git history | The Pod's Round 3 score is shared ([rubric](ROUND3-RUBRIC.md)) |

**No participant owns the final system alone:** orchestration, shared contracts, workflow state, integration, end-to-end testing, deployment, documentation, the demo and the submission are owned jointly by the Pod. **Specialist Pods:** the Specialist / Integration Engineer *leads and coordinates* that shared work and is **not** scored as having built a sixth agent.

If a Pod member does not contribute, tell an organiser early. Do not hide it in the demo.

## 2. Allowed technologies

- **Any language and framework** for your agent, *provided it honours the contract*: either in-process Python (`handle()`) or an HTTP service (`GET /health`, `POST /run`). See [`INTEGRATION-GUIDE.md`](INTEGRATION-GUIDE.md).
- **Any model provider** you have access to, with your own keys and at your own cost. Report `model.name`, `model.version`, `model.calls`, `model.cost_usd`.
- **The starter orchestrator is Python (FastAPI, httpx, jsonschema).** You may extend or replace it. Your replacement must still own the workflow state, produce a valid **Workflow State** and **Final Outcome**, and honour the fail-open, tenancy and never-hide-a-failure behaviour ([`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md)).
- **Open-source libraries** with licences that allow your use. Add them to `requirements.txt` (or your language's equivalent) so a clean clone runs.
- **Deployment** on any platform you can reach with a URL or a single command.
- **AI coding assistants are fine** (Round 2 itself asks for a `CLAUDE.md`). You are accountable for everything you commit, including understanding it well enough to explain it in the demo.
- **Not allowed:** training or tuning on the sample CSVs as if they were ground truth (they are synthetic); scraping or accessing systems you are not authorised to; real customer or seller data without permission; anything that attacks the organisers' infrastructure or other Pods.

## 3. Repository rules

| # | Rule | Enforced by |
|---|---|---|
| **R1** | Your Pod works in **one repository: your Pod's fork of the official starter** (or the copy the organisers tell you to use). That repository is your Round 3 development and final submission repository. Do not split the Pod's work across five forks. | Pod |
| **R2** | Do not edit, delete or interfere with the organisers' starter, **another Pod's repo**, or anyone's Round 2 repo. Your Round 2 repo stays as it was submitted. | Participant responsibility |
| **R3** | Bring your Round 2 agent into `agents/<name>/` by **copying** it. Record its origin in `PROVENANCE.md`. Adapting it for the contract is expected. | Pod / review |
| **R4** | Every member works under their own GitHub account. Contributions are assessed from history and PRs. | Repository history |
| **R5** | `main` must be runnable at all times after the first working integration. Merge through reviewed PRs ([`GITHUB-GUIDE.md`](GITHUB-GUIDE.md)). | Pod convention, read in history |
| **R6** | **No force-pushes to `main`.** No rewriting shared history. | Review of history |
| **R7** | Do not modify `data/sample/` (the organisers' synthetic data). Put your data in `data/input/`. | Review |
| **R8** | The Evidence Contract envelope is organiser-owned. Add to `payload`; propose anything else with a `contract` issue. Changes to `shared/` and `orchestration/` are discussed as a Pod before merging. | Tests + review |
| **R9** | Keep the repo light: no large binaries, model weights or datasets. | Review |

## 4. Code and commit rules

- **Small commits, meaningful messages** (what and why). `update`, `fix`, `final` and `final2` are not messages.
- **Tests stay.** Do not delete or weaken a test to turn CI green; fix the cause, or explain the change in `docs/decisions.md`.
- **`make test` passes** before you push; CI must be green before you merge.
- **Reviewed PRs.** Nobody merges their own PR unreviewed.
- **Say what you built.** Comments, docs and `agent.json` describe the real behaviour. A stub is labelled a stub.
- **Look up authoritative rules.** Where the channel publishes a requirement, retrieve it and record the source. Do not let a model recall it from memory, and do not infer it from examples. That includes the sample CSVs, whose requirement flags and fee amounts are dummy values.

## 5. Engineering rules (not negotiable)

These carry over from Round 2 and now apply to the **whole system**, not just your agent.

1. **Tenancy isolation before any feature.** `org_id` is on every request and record. A second organisation must see zero rows, and must not be able to fetch another organisation's image by guessing a key. The orchestrator rejects evidence about another tenant; **your storage must enforce it too.** Use the two demo orgs (`org_demo_alpha`, `org_demo_bravo`) for the test.
2. **Batch your model calls.** One call per unit carrying all checks, never one call per check. Report `model.calls`.
3. **Fail open.** A model error or timeout still saves the capture and still produces a record, marked `pending`. Nothing blocks the operator. The same holds for the system: a dead agent is **recorded as an error** and the workflow ends `FAILED`/`INCOMPLETE`: not a crash, and never a success.
4. **UNCERTAIN is a valid verdict.** It is not a low-confidence PASS. It is preserved as evidence, never silently becomes PASS or FAIL, reaches a human, and is shown in the demo.
5. **Look authoritative rules up.** See section 4.

### Evaluation rules (carried over from Round 2, now applied to the system)

- **Evaluation is assessed.** It must show whether your agents and your system perform their checks reliably, including uncertainty and review handling.
- **Use unseen data.** Where a check is vision-based, evaluate on a held-out set you did not tune against: recommended **at least 50 unseen units**, labelled **independently by two humans**, with their agreement measured where practical, varied image conditions, and genuinely ambiguous cases.
- **Report per check:** false positives and false negatives separately, the `UNCERTAIN` / review rate, important failure modes, and latency and cost where relevant. Recovery reports **claim precision** (a wrongly filed claim costs a seller standing; a missed one costs only money).
- **Say how the numbers were calculated.** Do not report only selected examples that make the system look successful.
- **System level (new in Round 3):** also report end-to-end outcomes by type, the share that needed a human, and how the system behaved under failure injection.

## 6. Honesty rules (assessed)

- **Say what you built, not what it sounds like.** You have a content hash. You do not have a tamper-evident, immutable or anchored record, unless you built one and can show it.
- **Overrides are data.** When an operator disagrees with the agent, capture the original verdict, the new verdict and a reason. Never discard those rows silently.
- **"It works well" is not a result.** Report a number per check, with false positives and false negatives separately, and write the method down. An honest 61% you can break down beats a 95% you cannot.
- **Contradictions are findings.** Where the documents or data disagree ([`docs/decisions.md`](docs/decisions.md)), raise it and state your assumption. Do not silently pick a side.
- **A stub passing is not your agent passing.** Do not present organiser stub behaviour as your own result.

## 7. Security and secrets rules

- **No secrets in the repo.** No API keys, tokens, passwords, private keys or `.env` files. Use environment variables; list the names in `.env.example`. CI blocks committed `.env` files and common key patterns, but it is a backstop, not a guarantee.
- **A leaked key is revoked, and it may affect the submission.** If you push one: **revoke it immediately**, then clean up. Deleting the commit does not make an exposed credential safe.
- **No real customer, seller or supplier data** unless you have the right to use it. Use synthetic or consented data.
- **No secrets, tokens or image bytes in logs or records.**
- **Do not expose credentials in screenshots, recordings or the demo.** Check your terminal, `.env` and browser tabs before you share your screen.
- **Do not hardcode production credentials.** Use `.env` locally and `.env.example` (placeholders only) for the variable names. **Remove debug credentials and test tokens before the final submission.**
- **Deployed demos** must not expose secrets, an unauthenticated admin surface, or one tenant's data to another.
- **Report a vulnerability** in the starter to an organiser rather than exploiting it.

## 8. Submission rules

- One submission **per Pod**, from the Pod's fork, as described in [`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md).
- You submit a **tagged commit** (`round3-final`). What is on that commit is what is assessed.
- Every link you submit must work **without logging in to your personal accounts**. Check it from a private window.
- The demo you submit must be of the system in that commit, working. A mock-up is not a demo.
- **LinkedIn post: mandatory (carried over from Round 2; the organisers will confirm it for Round 3).** Publish a post about your Pod's Round 3 build that says what you built and tags **CodeQuesters** and **Sydon.AI**, and include its URL in the submission form. The organisers share the official post template separately (**TBA**).

## 9. Deadline and finality rules

| | |
|---|---|
| Round 3 build phase begins | **TBA** |
| Submissions open | **TBA** |
| Final submission deadline | **TBA** |

- **All code commits that make up your Round 3 submission must be made during the authorised build phase.** Do not make code commits after the build phase ends.
- Carried over from Round 2, **unless the organisers say otherwise**: the submission form closes permanently at the deadline, and a submission is **final**: there is no reopening and no resubmission. Check everything before you submit.
- Late or incomplete submissions are assessed on what was submitted by the deadline.
- If a deadline-day problem is on the organisers' side (form down, repo access), tell an organiser *before* the deadline, in writing.
