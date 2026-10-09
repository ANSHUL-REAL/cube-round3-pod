# Evaluation (Pod 12)

Short, numeric and honest. Every number here comes from a file in this repo or from a run that anyone can repeat;
nothing is typed in by hand. What we did **not** measure is listed as plainly as what we did.

| Part | Source | Repeat it |
|---|---|---|
| System, claims, failure injection, tenancy | `docs/eval/system.json` | `python scripts/evaluate.py` (no key, no network, about 2 min) |
| Per check, Pack, real photos | Round 2 frozen held-out run, same engine | `python eval/metrics.py --run abid-test-v1` in the Round 2 repo |
| Per check, every photo agent, our own photos | `docs/eval/checks.json` (not produced yet) | `python scripts/score_checks.py sheet ...` then `score ...` |
| Real model calls through this repo | `docs/REAL-RUNS.md` | `python -m agents.<stage>.check ...` |

## Method

- **System runs: all 100 sample units** (`data/sample/cases.json`: 47 FBA, 20 merchant-fulfilled, 15 FBA returned,
  9 merchant-fulfilled returned, 9 route unknown). Receiving, Prep, Pack and Returns **replay the organisers' recorded
  evidence** (the labelled stubs in `tests/stubs/`), so these runs test the orchestrator, the hand-offs, the roll-up and
  **our real Recovery agent**, not the vision agents. That is a limit, stated again below.
- **Failure injection:** the same 100 units, five ways of breaking each of the five stages (down, timeout, invalid
  output, crash, evidence about another organisation): 25 injections, 2,500 workflows (1,575 of them reach the broken stage).
- **Per check on real photos (Pack only so far):** the Round 2 frozen run, 50 **unseen** real warehouse photos from the
  public Amazon Bin Image Dataset, held out from tuning, settings and photo hashes committed before the run. Pack's
  decision engine in this repo is that engine copied unchanged (checked file by file against commit `ef9e958`, see
  `agents/pack/PROVENANCE.md`), with the same model (`gemini-3.5-flash-lite`) and prompt (`pack-v3`).
  **Labels:** Amazon's own records of what is in each bin, not two independent people, so there is no human-agreement
  figure for it. The scorer for two people's labels exists (`scripts/score_checks.py`, Cohen's kappa) and has not been
  run on labelled photos yet.

## Per check

**Pack, 50 real held-out photos** ("problem" = the check should fail; UNCERTAIN is counted, never dropped):

| Check (unit of counting) | n | Problems caught (TP) | Correct passes (TN) | False alarms (FP) | Missed problems (FN) | UNCERTAIN on a problem | UNCERTAIN on an OK item | UNCERTAIN rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| items present (per order line) | 106 | 3 | 42 | **16** | **2** | 8 | 35 | 41% |
| quantities correct (per line whose item is in the box) | 93 | 4 | 22 | **18** | **0** | 3 | 46 | 53% |
| no extra / wrong item (per box) | 50 | 6 | 29 | **4** | **7** | 0 | 4 | 8% |

Box level: false SEAL (a wrong box would ship) 2/26, false STOP (a good box stopped) 12/24, UNCERTAIN 18/50. With the
same photos and **perfect perception** the rules make 0 errors, so every error is perception, not the rules. A kill
condition we set in advance tripped, so on photos like these Pack records evidence and a person decides; it must not
gate sealing.

**Receiving, Prep, Returns: not measured on labelled photos.** Each has made real Gemini calls on real photos
(`docs/REAL-RUNS.md`), but a handful of runs is not a rate. To measure: run the live demo units with the pod's own
photos, then `python scripts/score_checks.py sheet --store <data>/out` gives a sheet with the agent's verdict hidden; two
people label it independently; `score` prints TP / TN / FP / FN / UNCERTAIN per check and the two people's kappa.

**Recovery, per charge (100 units, 61 fee lines, our real agent):** CONTRADICTS 4, SUPPORTS 1, SILENT 56. The 56
silent lines are 42 weight-tier fees (no measurement and no sourced fee schedule, F-07), lost-inbound and
refund-without-return lines (F-10, F-11), and charges whose upstream evidence is uncertain or conflicting.

## System (100 units, replayed evidence + our Recovery)

| Final outcome | Units |
|---|---:|
| CLEAN | 33 |
| EXCEPTION | 54 |
| NEEDS_REVIEW | 9 |
| CLAIM_RECOMMENDED | 4 |

Status: COMPLETED 80, BLOCKED 20. **Needing a person: 20 of 100.** By route: FBA 11 clean / 27 exception / 6 review /
3 claims; FBA returned 3 / 10 / 1 / 1; merchant-fulfilled 11 / 9 / 0 / 0; merchant-fulfilled returned 4 / 5 / 0 / 0;
route unknown 4 / 3 / 2 / 0. Every route reaches a final outcome.

**Failure injection (1,575 workflows): 0 crashes, 0 reported as clean.** (2,500 run in total; 1,575 reach the broken stage.) In every one of the 25 injections, every
affected workflow recorded the error against the right stage and ended FAILED or INCOMPLETE (Receiving and Recovery
100 of 100 each time, Prep 62, Returns 24, Pack 29: the units on which that stage runs).

**Tenancy:** each of the five real agents, asked about another organisation's unit, refuses it (LookupError, HTTP 404).
The store and the orchestrator refuse evidence about the wrong organisation too (the `wrong_tenant` injection above).

**Live agents with no photos:** 100 workflows, 215 photo steps, every one answered `no_capture`; **0 verdicts invented
without a photo**; all 100 workflows end FAILED, which is the honest result.

## Claims (Recovery): precision first

4 claims, $6.00 in total, all `inbound_defect_fee`, each resting on a Prep record that passed and a Receiving record
with no damage, both captured before the charge: UNIT-0014 $2.00, UNIT-0026 $1.00, UNIT-0061 $2.00, UNIT-0071 $1.00.

**Precision against independent truth: not measured.** The sample is invented data with no labels (its README says not
to treat it as ground truth). What we can show: against the organisers' reference roll-up
(`data/expected/final-outcomes.sample.json`) our system has the same status and outcome on 97 of 100 units (measured by
`scripts/evaluate.py`, listed under `vs_reference` in `docs/eval/system.json`). On the other 3 (UNIT-0018, UNIT-0074,
UNIT-0095, $2.00 in total) the reference claims and **we do not**: Receiving recorded carton or unit damage (0074, 0095)
or a quality flag (0018) on those units, so a Prep pass does not prove the fee was wrong; Recovery records UNCERTAIN and
the workflow waits for a person (decision D-RC06). Agreement with the reference is not accuracy. We would rather miss a
claim than file a wrong one. Nothing is ever claimed on SILENT evidence.

## Cost and latency

| Stage | Model calls per unit | Measured time |
|---|---:|---|
| Pack | 1 | real photos: p50 4.6 s, p95 10.4 s (Round 2 run, 50 photos); 4.2 s and 10.0 s in this repo |
| Receiving | 1 | 4.0 s, 7.7 s (two real runs) |
| Prep | 1 | 8.5 s (one real run) |
| Returns | 1 (0 when the product has no reference photo) | 11.6 s (one real run) |
| Recovery | 0 (rules) | median 3 ms |
| Orchestration, replayed evidence | 0 | 100 workflows in about 2 s (1.98 s in the committed run) |

Tokens per Pack call: 2,439 on average (Round 2 run). USD: the key is on Gemini's free tier, so the cost field is left
empty (`cost_usd: null`) rather than guessed; it is filled from `COST_PER_1M_*` when those are set.

## Failure modes (named, with examples)

1. **Overconfident visibility (Pack):** the model says the whole box is visible and misses an item under straps or
   other items, so a good box is stopped (7 of the 12 false STOPs).
2. **Look-alikes (Pack):** a real product taken for a similar-looking one, read as "extra" (3 of the 12 false STOPs);
   the two false SEALs are a similar-name swap the model matched to the ordered look-alike (T06) and an extra product it
   did not see (T10).
3. **Counting (Pack):** quantities is the weakest check (53% UNCERTAIN).
4. **Unclear photos (Receiving):** "clarity 0.20, minimum 0.50": every check UNCERTAIN, sent to a person. Correct, but it
   means bad photos cost a person's time.
5. **No reference, no judgement (Returns):** an organiser SKU with no "as sold" photo cannot be judged
   (`no_product_reference`, no model call). Fixed per product by onboarding one photo.
6. **Photo reuse (Pack):** a photo already used for another order is refused, which is right, and surprises a demo that
   reuses one photo.
7. **Charge dates (Recovery):** evidence captured after a charge cannot prove that charge, so live photos taken today never
   support a claim on July's sample fees.

## Limits

- No per-check numbers on labelled photos for Receiving, Prep or Returns, and no human-agreement figure for any agent.
- The system numbers above use replayed evidence for four stages; they test integration and Recovery, not vision.
- The sample data is invented and unlabelled, so claim precision against the truth is unknown.
- Pack's per-check numbers come from warehouse bins, not packing-bench boxes.
- Fee rules and prep rules were not looked up from Amazon's published sources: every rule source is recorded as
  unverified, and Recovery stays SILENT where a rule would be needed (F-07).
