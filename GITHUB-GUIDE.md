# GitHub Guide

Round 3 is a **Pod build**: five people, one repository. In Round 2 you worked alone in your own fork. This round is the opposite: you share one repo and have to work in it without breaking each other's work.

```text
Official starter  (Cube-Build-A-Thon/cube-round3-pod)
        │  fork / copy
        ▼
Your Pod's fork   (one repository per Pod)   ◀── branches and PRs happen here
        │
        ▼
Final submission = the tagged commit `round3-final` on main
```

## 0. One-time setup

You need `git`, Python 3.11+, and a GitHub account.

```sh
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"     # the email on your GitHub account
gh auth login                                        # or check SSH:  ssh -T git@github.com
```

## 1. Fork the starter

Each Pod works in **one fork of the official starter**: [`Cube-Build-A-Thon/cube-round3-pod`](https://github.com/Cube-Build-A-Thon/cube-round3-pod). Do this once, together:

1. **One member** (agree who) forks the starter into their account, or uses **Use this template** on the starter's page for a copy (a template copy can be private; a fork of a public repo is public, so never put anything sensitive in it).
2. **Add the other four members as collaborators with write access** (Settings → Collaborators).
3. **Add the organisers as collaborators (read access is enough)** and send them the repository link, so your work can be assessed. The organisers will tell you which account to add.
4. Everyone clones **that one repository**:

```sh
git clone git@github.com:<owner>/<your-pod-repo>.git        # SSH
git clone https://github.com/<owner>/<your-pod-repo>.git    # or HTTPS
cd <your-pod-repo>
make setup && make test && make run      # everything should pass on the stubs before you change anything
```

**Do not work in five separate forks.** One shared repository, branches and pull requests inside it.

In your first session, together, make one commit that fills in `pod.json` (everyone's GitHub handle and agent, and your orchestration coordinator) and `.github/CODEOWNERS`. **Specialist Pods** (no Prep Manager): set `"flow": "orchestration/flow.specialist.json"` and `"pod_type": "specialist"` in `pod.json`, and remove the Prep member.

## 2. Who works where

| Path | Who | Others may |
|---|---|---|
| `agents/receiving/` | Member 1 | Review. Change only with the owner's OK. |
| `agents/prep/` | Member 2 (unused in Specialist Pods) | same |
| `agents/pack/` | Member 3 | same |
| `agents/returns/` | Member 4 | same |
| `agents/recovery/` | Member 5 | same |
| `orchestration/`, `shared/`, `tests/e2e/`, `data/input/` | **The Pod, together** | Anyone may propose; **discuss before merging** |
| `docs/`, top-level `*.md` | Everyone | Edit your own sections |

If everyone stays in their own folder, most merge conflicts never happen. The shared files are where conflicts come from, so keep changes there small and frequent.

## 3. Branches

One branch per area, short-lived:

```text
feature/receiving      feature/prep      feature/pack      feature/returns      feature/recovery
feature/orchestrator   (and feature/<area>-<topic> for a second piece of work, e.g. feature/orchestrator-resume)
```

- **`main` is always runnable.** Never push straight to `main` once the Pod has started.
- Commit small and often, with messages that say what and why:

```text
Good                                          Avoid
Map Receiving output to Agent Output          update
Block workflow on UNCERTAIN prep (D-004)      fix
Add wrong-tenant e2e test                     final / final2
```

- **Commit under your own name.** The history is how the organisers see who did what. Do not squash a teammate's work into your commit.

## 4. Pull requests

```text
Participant branch  ──▶  Pull Request  ──▶  Pod review  ──▶  Merge
```

```sh
git checkout main && git pull
git checkout -b feature/receiving
# … work …
make test                                  # before every push
git add -A && git commit -m "Map Receiving output to Agent Output"
git push -u origin feature/receiving
gh pr create --fill                        # the PR template has a checklist
```

1. **Every PR is reviewed by someone else.** Reviewing is part of *Team collaboration* in the [rubric](ROUND3-RUBRIC.md).
2. **CI must be green** (it runs `pytest` and a secret check). Do not merge red; do not delete a failing test to make it green.
3. Squash-merge into `main`, then delete the branch. Keep PRs small: if a review takes more than 15 minutes, split it.
4. A PR that records a design choice also adds a `docs/decisions.md` entry.

### Shared-file changes

Changes to **shared schemas, contracts, orchestration or common utilities** (`shared/`, `orchestration/`) affect every agent. **Discuss them as a Pod before merging** (a message in your channel is enough, a PR comment thread is better), and have at least one other member from a *different* agent review them. The contract envelope itself is organiser-owned: add to `payload`, or open a `contract` issue ([`EVIDENCE-CONTRACT.md`](EVIDENCE-CONTRACT.md) section 11).

> Free private repos cannot enforce required reviews, so all of this is a **convention**. It is still assessed through the PR history. Do not bypass it.

## 5. Conflict resolution

```text
Pull latest main  →  Resolve conflicts locally  →  Run tests  →  Push updated branch  →  Request review
```

```sh
git fetch origin
git rebase origin/main          # or: git merge origin/main (use one consistently within the Pod)
# on a conflict:  open the file, find <<<<<<<  =======  >>>>>>>, keep what is right (often BOTH), remove the markers
make test                       # the merge must still pass
git add <files> && git rebase --continue
git push --force-with-lease     # only on YOUR OWN branch after a rebase. Never force-push main.
```

| Conflict in | What to do |
|---|---|
| Your own `agents/<name>/` | You are the owner: resolve it. |
| Someone else's `agents/<name>/` | You should not be there. Talk to the owner. |
| `docs/build-log.md`, `docs/decisions.md` | Keep **both** sides; reorder. |
| `shared/schemas/*`, `orchestration/*` | Stop. This is a contract or design conflict, not a text conflict. Discuss as a Pod. |
| `data/sample/*`, `data/expected/*` | Do not modify the organisers' data. Put yours in `data/input/`. |

Stuck? `git rebase --abort` (or `git merge --abort`) puts you back where you started, with nothing lost.

## 6. Final repository requirements

```text
[ ] main passes CI and `make test` on a clean clone
[ ] Tag `round3-final` on the exact commit you submit      git tag round3-final && git push origin round3-final
[ ] README.md accurate for YOUR Pod (run steps, env vars, links to demo and deployment)
[ ] ARCHITECTURE.md describes your system; docs/decisions.md and docs/build-log.md up to date
[ ] .env.example complete; NO secrets in the repo or its history
[ ] Each member's work visible in the history under their own account
[ ] No large binaries or datasets; no real customer data; no force-pushes to main
```

**No code commits after the deadline.** See [`RULES.md`](RULES.md).

## 7. Common problems

| Symptom | Fix |
|---|---|
| `Repository not found` / `Permission denied` | Wrong GitHub account, or the repo owner has not added you as a collaborator. Ask the owner. |
| `rejected … (fetch first)` | `git pull --rebase`, resolve, push. |
| Passes locally, fails in CI | CI uses Python 3.12 and `requirements.txt`: add what you installed there. |
| CI says "Committed env file" / "Possible secret" | Remove it, **revoke the credential immediately** (deleting the commit does not make it safe). |
| I pushed to `main` by mistake | Do not force-push. Tell the Pod and `git revert <sha>` in a PR. |
| The data or docs contradict each other | Open a `finding` issue. Do not silently edit the shared data. |

## Never commit secrets

API keys, tokens, passwords, `.env` files, private credentials, real customer data. Use environment variables ([`.env.example`](.env.example)). If one slips into a commit, **revoke it first**, then clean up.
