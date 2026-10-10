# Who built what

Pod 12, five people, one agent each. This page says whose work each part is, and where to check it.

**Read this before counting commits.** Most commits here were pushed from the coordinator's account, because the
integration was done on one machine. Git authorship alone therefore undercounts the agent owners: each agent's design,
and for some its code, is its owner's Round 2 work. Each agent's `PROVENANCE.md` records what was carried over and
what changed, and [`docs/build-log.md`](docs/build-log.md) records who did each session.

| Member | GitHub | Agent | Whose work it is | Their own commits and pull requests here |
|---|---|---|---|---|
| Sai Charan | [@cherryy-x23](https://github.com/cherryy-x23) | Receiving | His Round 2 Receiving Manager ([repository](https://github.com/cherryy-x23/cube26-rcv-0095-cherryy-x23)) is the design the running agent keeps: the model observes, fixed rules decide. Ported for him by @ANSHUL-REAL ([provenance](agents/receiving/PROVENANCE.md)). He also wrote a Round 3 implementation, kept in the tree unchanged. | `e1ce387`, pull request #1 on the Pod's fork: `agents/receiving/core/` and `agents/receiving/app_pr1.py` (5 files, 1,850 lines). Decision [D-V07](docs/decisions.md). |
| Devisri | [@Devisri-074](https://github.com/Devisri-074) | Prep | Owner of the Prep agent: the Python port of the organisers' reference Prep ([provenance](agents/prep/PROVENANCE.md); build log, 2026-10-07; decisions D-PR01 to D-PR07). | `5267e03`, pull request #2: an agent that cannot be reached is reported as unavailable, not as slow (`orchestration/clients.py`). |
| Anshul Nautiyal | [@ANSHUL-REAL](https://github.com/ANSHUL-REAL) | Pack, and coordinator | His Round 2 Pack Manager, core copied unchanged ([provenance](agents/pack/PROVENANCE.md)). Integration of the five agents, the orchestrator hardening, the console, the phone stations, the deployment, and the ports noted in this table. | The remaining commits. |
| Krishna Babu | [@krishnababuprodduturu](https://github.com/krishnababuprodduturu) | Returns | His Round 2 Returns Manager: the judgment engine in `agents/returns/core/`, the rubrics, policies, product cards and the hash-locked prompt are his files, copied with only import lines changed ([provenance](agents/returns/PROVENANCE.md)). Brought into the contract for him by @ANSHUL-REAL. | `95a3548`, pull request #1: `agents/returns/returns_manager/`, his full Round 2 product, kept as reference (decision D-RT11). |
| Sai Tarun Reddy Velagala | [@DaKaufeeBoii](https://github.com/DaKaufeeBoii) | Recovery | His Round 2 Recovery Manager supplied the rules: claim only when evidence contradicts a charge, precision first. No Round 2 file was copied; the rules were rebuilt on the organisers' fee report by @ANSHUL-REAL ([provenance](agents/recovery/PROVENANCE.md)). | None in this repository. |

The starter this repository is forked from is the organisers' (`Sohan5RC`, three commits).

## Where each person's work shows in the running system

- **Receiving** (`agents/receiving/app.py`, `rules.py`, `vision.py`): 54 tests in `tests/integration/test_receiving_agent.py`.
- **Prep** (`agents/prep/`): 70 tests in `tests/integration/test_prep_agent.py`.
- **Pack** (`agents/pack/`): 24 tests in `tests/integration/test_pack_agent.py`.
- **Returns** (`agents/returns/`): 50 tests in `tests/integration/test_returns_agent.py`, 13 in `test_returns_onboard.py`.
- **Recovery** (`agents/recovery/`): 83 tests in `tests/integration/test_recovery_agent.py`.
- **Orchestration, console, deployment** (`orchestration/`, `shared/`): owned jointly by the Pod
  ([`.github/CODEOWNERS`](.github/CODEOWNERS)); `pod.json` names the coordinator.
