# Life Sciences RAG PoC

[中文版 README](README.zh-CN.md)

A knowledge retrieval and assisted-drafting system for **Medical Information (MI)** and **Pharmacovigilance (PV)** scenarios, modeled on Veeva Vault / Vault Safety workflows. Built on Milvus + MySQL + Neo4j + BGE embeddings, with a local LLM drafting layer.

**MySQL is the source of truth; Milvus is a shadow index.** Full texts live in MySQL with an `embedding_updated_at IS NULL` pending-sync marker; sync is idempotent and re-runnable. The shadow index holds only `status='current'` content — retired/merged/draft documents stay in MySQL for audit and never enter retrieval.

## Document Types & Authority Levels (Class A: curated, version-managed)

| Collection | Document Type | Authority | Retrieval |
|---|---|---|---|
| drug_inserts | Drug labels (incl. dual-version revision drill) | A (highest) | Keyword-exact |
| guidelines | Clinical guidelines/consensus (chunked per recommendation) | A- (quasi-authoritative) | Vector semantic |
| clinical_papers | Clinical papers | B (reference) | Vector semantic |
| case_reports | Case reports / adverse events | C (empirical) | Hybrid |

Version management: retired versions stay in MySQL for audit; the shadow index removes them by ID. Regression includes **version assertions** to prevent stale-version leakage.

## Event-Stream Data (Class B: dedup + human confirmation)

| Collection | Data | Dedup Logic |
|---|---|---|
| mi_inquiries | Medical inquiries (hotline/WeChat/field relay) | ≥0.85 auto-duplicate (count+1) / 0.70–0.85 human review / <0.70 new question (draft) |
| case_reports | Multi-channel case intake (GVP scenario) | Vector pre-screen + four-element rule scoring (drug 50% / onset window 30% / patient 20%); hybrid ≥0.87 merge / 0.78–0.87 human gate / <0.78 new case |

Inquiries pass through a **normalization layer** (`src/normalization.py`) before embedding: brand names / colloquial names / typos → generic names (Lipitor→atorvastatin, Glucophage→metformin, ...). `status=draft` inquiries are excluded from both retrieval and the dedup baseline pool (`status=='approved'` filter).

## Validation Layer (independently deliverable)

Three components that make LLM/RAG output verifiable — designed to work as a standalone service for any RAG pipeline:

- **PII redaction** (`src/pii_guard.py`) — five-class detection (phone/ID/email/MRN/address) plus rule-based Chinese name detection; validated by a 20-probe adversarial suite (`scripts/test_pii.py`)
- **Citation assertion middleware** (`scripts/validate_citation.py`) — verifies every citation in an LLM draft against the retrieved chunks; CLI exit code 0/1, batch JSONL mode, CI-ready
- **Customer threshold calibration service** (`scripts/import_calibration_pairs.py` + `scripts/calibration_report.py`) — customer-labeled pairs (CSV/JSON) → distribution analysis + threshold recommendation report (Markdown/JSON); final gating by golden-set regression

## Quick Start

```bash
# 1. Start dependencies (Milvus :19531 / Neo4j :7687 / MySQL :3307)
docker compose up -d

# 2. Ingestion (simulated multi-format intake: PDF/CSV/TXT/XML → MySQL → Milvus)
python scripts/make_inbox_samples.py
python scripts/ingest.py

# 3. Seed document data: MySQL load + Milvus sync (idempotent)
python scripts/migrate_to_mysql.py            # full; --seed-only / --sync-only

# 4. Initialize specialized collections (idempotent)
python scripts/init_inquiries.py              # inquiry base (--rebuild to reset)
python scripts/init_guidelines.py             # guidelines (with version retirement)
python scripts/init_cases.py                  # multi-channel case dedup
python scripts/init_drug_versions.py --phase chaos   # dual-version: create stale-version chaos
python scripts/init_drug_versions.py --phase retire  # revision takes effect: retire old + shadow removal

# 5. Query & answer (--role: hotline/medical/pv, default general)
python scripts/query.py "阿托伐他汀和红霉素联用需要注意什么" --role hotline
python scripts/ask.py "阿托伐他汀和红霉素可以联用吗"    # graph + vector hybrid, with citations

# 6. Coverage reconciliation (drug gaps → backfill tickets)
python scripts/check_coverage.py

# 7. PV literature monitoring (LLM extraction → graph dedup → signal grading)
python scripts/pv_monitor.py                  # --dry-run to preview
```

## Verification (ingested ≠ effective; only regression-passed counts)

```bash
python scripts/regression.py       # golden-set gate: normalization/tier/drift/version assertions, exit 1 on failure
python scripts/test_inquiries.py   # inquiry demo tests: normalization/dedup tiers/permission filter/idempotency
python scripts/calibrate.py        # threshold calibration: 24 labeled pairs → two-cluster distribution + 3 candidate lines
python scripts/rebuild_shadow.py --model-path <model> --tag <tag>   # blue-green shadow rebuild for model swap (--cutover)
```

Golden-set probes: `config/golden_set.json` (append-only; every badcase fix becomes a probe). Runtime threshold source of truth: MySQL `threshold_config` table (changes via `scripts/set_threshold.py` with mandatory `--by`/`--reason`, audited in append-only `threshold_history`); `config/thresholds.yaml` is the seed for new environments + offline fallback.

## Model Setup

Embeddings: BGE family (current: bge-small-zh-v1.5). Download from HuggingFace and point the loader at your model repository:

```bash
export LS_RAG_MODELS_DIR=/path/to/models   # must contain bge-small-zh-v1.5/
```

Drafting LLM: local Ollama (qwen3:4b, fully offline) or any OpenAI-compatible endpoint (`.env` with `ARK_API_KEY` / `ARK_MODEL_ENDPOINT`; see `src/llm_config.py`).

## Documentation

| Document | Content |
|---|---|
| [docs/poc-overview-print.md](docs/poc-overview-print.md) | One-page printable overview + reference details |
| [docs/compliance-system-design.md](docs/compliance-system-design.md) | **Two-system master plan**: business pipeline vs verification/compliance system — components, change control, governance rules, gap register |
| [docs/design-overview-en.md](docs/design-overview-en.md) | **English design overview** (condensed) |
| [docs/vector-db-construction-design.md](docs/vector-db-construction-design.md) | Construction design + 15 measurement-driven design iterations (Ch. 6) |
| [docs/rag-engineering-standards.md](docs/rag-engineering-standards.md) | 17 engineering standards, each traced to a real incident |
| [docs/rag-industry-research.md](docs/rag-industry-research.md) | Design-basis research: Veeva US/China, medical RAG peers, US-China compliance gaps, validation-gate lineage, AI-output validation layer (ISPE five-layer mapping) |
| [docs/model-selection-recalibration.md](docs/model-selection-recalibration.md) | Embedding model selection & recalibration: two-stage process / China model comparison / exit criteria + Appendix C three-model benchmark |
| [docs/drill-customer-calibration-2026-08-25.md](docs/drill-customer-calibration-2026-08-25.md) | Customer calibration drill + negative-control experiments: deliberately injected bad thresholds to verify gate interception (gate sensitivity map) |
| [docs/validation/](docs/validation/README.md) | **GxP validation package**: RTM traceability matrix + IQ/OQ/PQ templates + signature page + automated evidence collection |
