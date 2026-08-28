# Design Overview (English, condensed)

> Condensed English edition of `vector-db-construction-design.md`. The full document (Chinese) includes 15 measurement-driven design iterations in Chapter 6; this overview covers the stable architecture and the reasoning behind it.

## 1. Problem Frame

Two regulated life-sciences workloads, modeled on Veeva Vault / Vault Safety:

- **Medical Information (MI)**: hotline/WeChat/field-relayed inquiries arrive in colloquial language with brand names and typos. Each must be deduplicated against historical inquiries, and answered with authority-graded evidence.
- **Pharmacovigilance (PV)**: adverse-event case reports arrive from multiple channels. GVP requires duplicate detection (same patient, same drug, same event = one case), and literature monitoring for signal detection.

Both share one hard requirement: **every automated decision must be reproducible, assertable, and auditable.** This rules out opaque end-to-end LLM pipelines and drives the deterministic-DAG architecture below.

## 2. Core Architecture: Source + Shadow

```
MySQL (source of truth)                Milvus (shadow index)
─────────────────────────              ─────────────────────
full text + metadata + audit    sync   vectors + minimal payload
status lifecycle: draft →       ───►   only status='current'
approved → retired/merged              enters retrieval
embedding_updated_at IS NULL  ◄───     idempotent, re-runnable
= pending-sync marker
```

Design principles:

1. **Ingested ≠ effective** — a document is retrievable only after regression passes. The shadow index is rebuilt or patched; the source is never mutated by sync.
2. **State machine = retrieval eligibility** — retired/draft content is physically absent from the shadow, not filtered at query time. Retrieval cannot leak stale wording by construction.
3. **One purpose, one vector** — dedup encodes only discriminative fields; display content goes to payload.
4. **Score is a gate, not a ranking** — above the threshold, ordering is a business problem (authority > role > version), not a similarity problem.

## 3. The Dedup Pipeline (MI inquiries)

```
raw inquiry (colloquial, brand names, typos)
  → ① normalization: brand/colloquial/typo → generic name (BEFORE embedding)
  → ② vector cosine vs approved baseline pool
       ≥ duplicate (0.85)  → auto-merge, occurrence+1, no human
       new (0.70) ~ duplicate → gray zone → human gate ①
       < new (0.70)  → new question, status=draft
  → ③ drug-set guardrail: score ≥ duplicate but drug sets differ
       → forced down to human gate (blocks only, never promotes)
  → ④ answer + review (draft → approved)  ← human gate ②
```

Key lessons baked in:

- **Normalization precedes embedding** — "Lipitor" and "atorvastatin" must be identical in vector space by construction, not by model luck.
- **The gray zone is an architectural necessity** — measurement showed the two clusters (same-question vs different-question) overlap; a single threshold cannot be both safe and automated. Bandwidth = staffing decision.
- **The guardrail is an independent defense layer** — in a negative-control drill, deliberately injecting a bad duplicate threshold (0.75) produced zero regressions because the drug-set guardrail caught all three gray probes. Defense-in-depth, empirically verified.

## 4. Case Report Dedup (PV): Vector + Rules

Pure vector similarity cannot capture regulatory identity. Two-stage hybrid:

```
① vector pre-screen (Milvus): semantic candidates
② rule scoring (MySQL source_meta): four regulatory elements
   drug-set overlap 50% + onset window 30% + patient profile 20%
   with three hard vetoes: sex mismatch / event mismatch / zero drug overlap
hybrid = 0.5 × vector + 0.5 × rules
   ≥0.87 merge / 0.78–0.87 human gate / <0.78 new case
```

The hard vetoes came from calibration: the initial intuitive thresholds (0.90/0.75) misjudged 5 of 14 labeled pairs — 4 of the 5 errors were **rule gaps, not threshold problems** (event not scored, sex mismatch only zeroing a 0.2-weight term, no veto on zero drug overlap). Thresholds cannot compensate for missing regulatory elements.

## 5. Threshold Calibration as a Managed Process

Thresholds are not universal constants; they are fitted to (model × data distribution) and must be recalibrated when either changes. The recalibration pipeline is a four-stage validation gate:

```
① shadow rebuild (blue-green, scripts/rebuild_shadow.py)
② calibration on labeled pairs → distribution + 3 candidate lines (scripts/calibrate.py)
③ set threshold via audited write path (scripts/set_threshold.py --by --reason)
④ golden-set regression as final gate (scripts/regression.py, exit 1 on failure)
```

- **Cosine proxy sets initial values; regression sets final values** (observed deviation 0.05–0.10).
- **Exit criteria are relative, not absolute** — a model that cannot separate the hard negative (0.9585 for a one-character-different drug name) hits a physical limit of pure-vector dedup, not a tuning failure.
- Runtime source of truth is the MySQL `threshold_config` table (BYOM: business-side tuning without code deployment), with an append-only `threshold_history` audit table. `config/thresholds.yaml` is seed + offline fallback.
- Model identity is fingerprinted (SHA-256 of weights) and three-way consistency-checked: golden_set ↔ thresholds ↔ model directory.

## 6. Generation Layer: Assisted Drafting, Never Autonomous

LLM drafting is a gated stage, not a free generator. Five terminal dispositions, all traced:

```
question → PII redaction → dedup short-circuit → retrieval
  → eligibility gate (top1 < draft_min_top1 → refuse, LLM never invoked)
  → coverage check (drug missing A/A- source → escalate, no draft)
  → LLM draft (grounded template, citations [1]..[5])
  → citation assertion (every citation must resolve to a retrieved chunk)
  → bounded retry with error feedback
dispositions: standard_answer / draft_passed / insufficient_evidence
              / draft_failed_human / coverage_gap
```

Core lesson (ANS-03): a draft with formally valid citations can still be semantically unfaithful — **formal validity ≠ faithfulness** — so the eligibility gate is a deterministic pipeline stage, not an LLM self-judgment. Design choice: for combination questions where only half the drugs are in the library, no draft is produced at all (a half-supported draft risks being read as an endorsement).

## 7. Validation Layer (independently deliverable)

| Component | What it proves |
|---|---|
| PII redaction (`src/pii_guard.py`) | 20-probe adversarial suite: baseline / false-positive / miss-risk tiers |
| Citation assertion (`scripts/validate_citation.py`) | CLI middleware, exit 0/1, batch JSONL, CI-ready |
| Customer calibration service | customer-labeled pairs → recommendation report; regression has final say |

The full GxP validation package (`docs/validation/`): RTM traceability matrix, IQ/OQ/PQ controlled templates, and an executed pack (2026-08-18 model-swap requalification) with environment fingerprints and archived regression output. The machine collects evidence; only humans sign.

## 8. What Was Deliberately NOT Built

- **No LangGraph/ReAct orchestration** — the deterministic DAG is a compliance asset (reproducible, assertable, auditable). Agent-style orchestration becomes justified only for multi-turn clarification, literature triage loops, or complex case analysis.
- **No learning-to-rank / learned decision surfaces** — with <100 labeled pairs, statistical calibration wins on explainability, which is what GxP review demands.
- **No silent fallbacks** — every degradation path is explicit, traced, and routed to a human.
