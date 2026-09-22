# AI Agent Program Functional Verification Based on Intent Structuring and Metamorphic Testing

**A Study on Program Functional Verification with AI Agents based on Intent Structuring and Metamorphic Testing**

**English** | [繁體中文](README_zh-TW.md)

This repository provides a **public reference implementation** of the framework proposed in the master's thesis **“A Study on Program Functional Verification with AI Agents based on Intent Structuring and Metamorphic Testing.”**

The research aims to reduce the risk of **false-pass** outcomes when AI Agents generate programs under limited testing. The framework transforms natural-language requirements into a frozen acceptance specification, extracts and aligns candidate-program behavior with the expected behavior, and then combines basic functional probes, Metamorphic Testing (MT), defect evidence, and iterative repair to build a traceable verification workflow connecting requirements, program behavior, and runtime evidence.

> **Core principle: LLMs are used where semantic understanding or generation is needed; final verification outcomes are determined by executable rules and runtime evidence rather than by using an LLM as the final judge.**

## Framework Overview

```text
Natural-language Requirement
            │
            ▼
     Intent Structuring
            │
            ▼
       Frozen Spec
            │
            ▼
      Code Generation
            │
            ▼
   Behavior Extraction
            │
            ▼
   Behavior Alignment
            │
            ▼
 Verification Strategy
      (LLM-assisted)
            │
            ▼
Applicability / Executability
       Rule Validation
            │
            ▼
 Source + Follow-up Tests
            │
            ▼
    Controlled Execution
            │
            ▼
 Deterministic MR Checking
            │
      ┌─────┴─────┐
      ▼           ▼
    PASS       Defect Evidence
                    │
                    ▼
                  Repair
```

The workflow consists of the following modules:

1. `main.py`: accepts a natural-language requirement and starts the complete Stage 1–7 workflow.
2. `intent_structuring.py`: creates and independently reviews the `intent_spec`, which is frozen as the shared acceptance basis after passing review.
3. `code_generation_agent.py` + `stage2_server.py`: generate candidate Python programs through restricted MCP tools.
4. `behavior_extraction.py`: extracts candidate-program behavior evidence using AST and source-code rules.
5. `behavior_alignment.py`: compares expected behavior with structural evidence observed in the candidate program.
6. `verification_framework.py`: builds the verification profile, reviews executable MRs, and executes source/follow-up cases and relation checks in Docker.
7. `repair_feedback.py` + `repair_generation.py`: convert verification failures into structured defect evidence, perform repair, and re-run verification.

For a more detailed description of the LLM/rule separation, see [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md).

## LLM-Assisted and Deterministic Verification Components

### LLM-assisted components

- Natural-language requirement understanding and intent structuring
- Candidate-program generation
- Semantic assistance for selected verification-profile / candidate verification-strategy tasks
- Program repair based on structured defect evidence

### Deterministic verification components

- MR applicability and requirement-evidence checks
- Executability and input-validity checks
- Construction of source/follow-up test cases
- Controlled execution in Docker
- Runtime comparison according to the expected MR relation
- Traceable PASS / violation / insufficient-evidence records

**LLM assists semantic reasoning; executable rules and runtime evidence determine verification outcomes.**

### Original-I/O / CPE-style tasks

For standard input/output programming tasks, the current public reference implementation uses a conservative **registry-gated MR** design. An MR may enter executable verification only when it satisfies constraints based on requirement evidence, task characteristics, supported MR types, and available executable checkers. Free-form LLM MR suggestions are not used directly as executable verdict criteria.

The purpose of this design is to keep adopted verification relations traceable, executable, and comparatively stable, while retaining LLM capabilities in requirement understanding, generation, and repair.

> **Blind / sample-I/O note:** In the formal thesis benchmark, problem-provided sample inputs and outputs were masked before the experiment. The current reference implementation retains a generic helper for user-supplied requirements that still contain sample inputs: a cleaned sample input may be used only as a source seed for a generic MR. The sample expected output is used only by a basic sanity probe and is not the oracle for the final MR relation check. This helper does **not** imply that unmasked sample I/O was used in the historical thesis benchmark.

## Thesis Results — Primary CPE Evaluation

The following values are the **primary CPE results reported in the thesis**. They are provided as archived reference metrics and are not a claim that every future re-run must reproduce identical values.

| Metric | Baseline | Framework |
|---|---:|---:|
| Internal PASS | 726 / 1,012 (71.74%) | **968 / 1,012 (95.65%)** |
| Stratified independent test | 155 / 200 (77.50%) | **179 / 200 (89.50%)** |
| Sampled false-pass proportion | 45 / 200 (22.50%) | **21 / 200 (10.50%)** |

MR execution statistics for the CPE Framework:

- Approved MRs: **3,027**
- Executed MRs: **3,015**
- Execution completion rate: **99.60%**
- Average executed MRs per problem: **2.98**
- Observed MR violations: **15 (0.50%)**

The thesis also evaluates the framework on APPS Interview as a cross-dataset assessment. See [`docs/RESULTS.md`](docs/RESULTS.md) for the complete aggregated thesis results and interpretation limits.

## Important Reproducibility Note

This repository is a **public reference implementation of the framework**, not a bit-for-bit archival image of every historical thesis experiment environment.

Some stages use locally deployed large language models. Generated content may therefore vary with model builds, Ollama/Python/Docker versions, inference settings, and hardware. Thesis metrics should be treated as reference results obtained under the original experimental setup, not as exact regression targets that every future execution is guaranteed to reproduce.

See [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) for details.

## Requirements

- Python 3.11.15 (the version used in the thesis experiments; Python 3.11.x is the target compatibility range for this reference implementation)
- Conda (recommended)
- Ollama
- Docker Desktop / Docker Engine
- Python packages: `ollama`, `mcp`

### Thesis model configuration

The default model configuration in this public implementation is aligned with the thesis experiment setup:

| Role | Model | Purpose |
|---|---|---|
| Model A | `gemma4:31b` | Stage 1 intent-spec construction, Stage 2 code generation, Stage 5 verification-profile / MR semantic assistance, and Stage 7 program repair |
| Model B | `nemotron-cascade-2:30b` | Stage 1 independent requirement-consistency review; corresponds to **Nemotron-Cascade-2-30B-A3B** in the thesis |
| Embedding | `bge-m3` | Stage 1 vector semantic-similarity assistance |

Except for the independent Stage 1 review model and the embedding model, the main generation/semantic-reasoning roles that directly invoke an LLM in this reference implementation default to `gemma4:31b`. In the current public implementation, Stage 3 behavior extraction, Stage 4 behavior alignment, and Stage 6 defect-evidence organization are primarily handled through AST analysis, deterministic rules, and existing runtime evidence. Stage 1 Model A and Model B must use different models to preserve separation between generation and independent review.

## Installation

Create the Conda environment:

```powershell
conda env create -f environment.yml
conda activate thesis-framework
```

Or install only the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Prepare the Docker sandbox image:

```powershell
docker pull python:3.11-slim
```

Prepare the Ollama models:

```powershell
ollama pull gemma4:31b
ollama pull nemotron-cascade-2:30b
ollama pull bge-m3
```

Check the runtime environment and repository integrity:

```powershell
python preflight.py
python verify_release.py
```

`preflight.py` checks Python packages, Docker, the Ollama service, and required models. It does not start the thesis workflow.

## Configuration

The default Ollama API base URL is `http://localhost:11434`. The following environment variables can override the defaults:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Stage 1 Ollama base URL |
| `OLLAMA_CHAT_URL` | `${OLLAMA_BASE_URL}/api/chat` | Stage 1 / Stage 5 / Stage 7 chat endpoint |
| `OLLAMA_EMBED_URL` | `${OLLAMA_BASE_URL}/api/embed` | Embedding endpoint |
| `OLLAMA_EMBEDDINGS_URL` | `${OLLAMA_BASE_URL}/api/embeddings` | Legacy embedding endpoint |
| `STRUCTURING_MODEL` | `gemma4:31b` | Stage 1 Model A: intent-spec construction |
| `REVIEW_MODEL` | `nemotron-cascade-2:30b` | Stage 1 Model B: independent requirement-consistency review |
| `EMBEDDING_MODEL` | `bge-m3` | Stage 1 embedding model |
| `GENERATION_MODEL` | `gemma4:31b` | Stage 2 code generation |
| `REPAIR_MODEL` | `gemma4:31b` | Stage 7 program repair |

Runtime paths are resolved relative to the repository root. A relative path passed to `--requirement-file` is also resolved from the repository root.

## Quick Start

Run the complete Stage 1–7 workflow:

```powershell
python main.py --requirement-file examples/minimal_requirement.txt --mode repair-once
```

Run the repair loop:

```powershell
python main.py --requirement-file examples/minimal_requirement.txt --mode repair-loop --max-repairs 2 --generation-model gemma4:31b --mr-model gemma4:31b --repair-model gemma4:31b
```

Re-run Stage 3–7 for an existing RUN / GEN pair:

```powershell
python main_pipeline.py --run-id RUN-000001 --gen-id GEN-000001 --mode verify --mr-model gemma4:31b
```

After a complete `main.py` run, the Stage 1 and Stage 2 runtime paths are retained while an additional consolidated snapshot is created. Existing RUN / GEN artifacts can therefore still be re-verified through `main_pipeline.py`.

## Minimal Example

`examples/minimal_requirement.txt` is a minimal Counter-class requirement that is not part of the formal benchmark datasets. It is intended for the first environment check.

The complete workflow calls Ollama and starts Docker, so this is not a pure offline unit test.

## Output

Working directories created during execution:

```text
stage1_outputs/RUN-xxxxxx/
workspace/code_generation/RUN-xxxxxx/
workspace/behavior_extraction/RUN-xxxxxx/
workspace/behavior_alignment/RUN-xxxxxx/
workspace/verification/RUN-xxxxxx/
workspace/repair_feedback/RUN-xxxxxx/
workspace/main_runs/RUN-xxxxxx/
workspace/full_runs/
```

A consolidated snapshot is also created after a complete run:

```text
workspace/run/RUN-xxxxxx/
├── 01_stage1/
├── 02_code_generation/
├── 03_behavior_extraction/
├── 04_behavior_alignment/
├── 05_verification/
├── 06_repair_feedback/
├── 07_main_runs/
├── 08_full_runs/
├── 99_final/
└── run_manifest.json
```

The primary verification decision field is stored in `verification_summary.json`:

```text
final_decision_for_delivery = verification_passed_for_current_scope
```

## Project Structure

```text
intent-structured-metamorphic-verification/
├── main.py
├── main_pipeline.py
├── intent_structuring.py
├── code_generation_agent.py
├── stage2_server.py
├── behavior_extraction.py
├── behavior_alignment.py
├── verification_framework.py
├── repair_feedback.py
├── repair_generation.py
├── skills/
│   ├── skill_planner_intent.py
│   ├── skill_code_generation.py
│   ├── skill_verification_framework.py
│   └── skill_repair_feedback.py
├── examples/
│   └── minimal_requirement.txt
├── docs/
│   ├── FRAMEWORK.md
│   ├── RESULTS.md
│   └── REPRODUCIBILITY.md
├── CITATION.cff
├── environment.yml
├── requirements.txt
├── preflight.py
├── verify_release.py
├── .gitignore
├── README.md
└── README_zh-TW.md
```

## Repository Scope

To keep evaluation data separate from the framework implementation, this repository **does not include**:

- The official CPE or APPS datasets or isolated test data
- Historical `workspace` / `stage1_outputs`
- Per-problem experimental outputs, logs, or caches
- Ollama model weights or Docker images
- Checkpoints or IDE/local-environment settings
- The thesis PDF or presentation slides

Thesis results are presented only as aggregated reference metrics. Re-running the full benchmark requires independently obtaining data that you are legally permitted to use and applying the same isolation protocol.

## Citation

If this repository is used in research or derivative work, GitHub's **Cite this repository** feature can be used. Citation metadata is provided in [`CITATION.cff`](CITATION.cff).

## License

This repository currently **does not include an open-source license**. Public visibility does not automatically grant permission to redistribute, modify, or create derivative works. Any further use should comply with applicable copyright rules.
