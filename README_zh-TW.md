# 基於意圖結構化與蛻變測試之 AI Agent 程式功能驗證框架

**A Study on Program Functional Verification with AI Agents based on Intent Structuring and Metamorphic Testing**

本 repository 為碩士論文「**基於意圖結構化與蛻變測試之 AI Agent 程式功能驗證研究**」所提出框架的**公開參考實作（reference implementation）**。

研究目標是降低 AI Agent 生成程式在有限測試下產生 **False Pass** 的風險。框架將自然語言需求整理為固定驗收規格，抽取候選程式行為並與需求對齊，再結合基本功能探測、蛻變測試（Metamorphic Testing, MT）、缺陷證據與迭代修復，建立需求、程式行為與動態執行結果之間的可追溯驗證流程。

> **核心定位：LLM 負責需要語意理解與生成能力的工作；正式驗證結論則以可執行規則與實際執行證據為準，而不是直接由 LLM 充當最終裁判。**

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

完整流程由下列模組組成：

1. `main.py`：接收自然語言需求並啟動完整 Stage 1–7 流程。
2. `intent_structuring.py`：建立與獨立審查 `intent_spec`，通過後凍結為後續共同驗收基準。
3. `code_generation_agent.py` + `stage2_server.py`：透過受限 MCP tools 產生候選 Python 程式。
4. `behavior_extraction.py`：利用 AST 與原始碼規則抽取候選程式行為證據。
5. `behavior_alignment.py`：比較預期行為與候選程式觀察到的結構證據。
6. `verification_framework.py`：建立 verification profile、審查可執行 MR，並於 Docker 中執行 source / follow-up cases 與關係檢查。
7. `repair_feedback.py` + `repair_generation.py`：將正式驗證失敗轉換為結構化缺陷證據，提供修復並重新驗證。

更完整的 LLM／規則分工請見 [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md)。

## LLM 與 Deterministic Verification 的分工

### LLM-assisted components

- 自然語言需求與意圖結構化
- 候選程式生成
- 部分驗證 profile / 候選驗證策略的語意輔助
- 根據結構化缺陷證據進行程式修復

### Formal verification components

- MR 適用條件與需求證據檢查
- 可執行性與輸入合法性檢查
- source / follow-up 測試案例建立
- Docker 受控環境執行
- 依 MR 定義比較實際輸出關係
- 產生正式 PASS / violation / insufficient-evidence 等驗證證據

**LLM assists semantic reasoning; executable rules and runtime evidence determine formal verification outcomes.**

### Original-I/O / CPE 類型

對標準輸入輸出型任務，目前公開參考實作採取較保守的 **registry-gated MR** 設計：可正式執行的 MR 必須通過題目敘述證據、任務型態、已支援 MR 類型與可執行檢查器等限制。自由形式的 LLM MR 建議不會直接進入正式執行。

此設計的目的，是讓正式驗證關係具有可追溯、可執行與較穩定的判定依據，同時保留 LLM 在需求理解、生成與修復階段的能力。

> **Blind / sample-I/O 說明：** 論文正式 benchmark 於實驗前遮蔽題目所附範例輸入輸出；目前 reference implementation 則保留一個通用 helper：若使用者自行提供的需求文字仍含 sample input，可將經清理的 sample input 僅作為 generic MR 的 source seed。sample expected output 只屬於 basic sanity probe，不作為 formal MR oracle。這個 helper 不代表論文歷史 benchmark 曾使用未遮蔽 sample I/O。

## Thesis Results — Primary CPE Evaluation

以下為論文正式實驗中所報告的 **CPE 主要結果**，用作 archived reference metrics，而不是宣稱未來每次重新執行都必須得到完全相同數值。

| Metric | Baseline | Framework |
|---|---:|---:|
| Internal PASS | 726 / 1,012 (71.74%) | **968 / 1,012 (95.65%)** |
| Stratified independent test | 155 / 200 (77.50%) | **179 / 200 (89.50%)** |
| Sampled false-pass proportion | 45 / 200 (22.50%) | **21 / 200 (10.50%)** |

CPE Framework 的 MR 執行情形：

- Approved MRs: **3,027**
- Executed MRs: **3,015**
- Execution completion rate: **99.60%**
- Average executed MRs per problem: **2.98**
- Observed MR violations: **15 (0.50%)**

論文亦使用 APPS Interview 進行跨資料集評估。完整論文彙整數據與解讀限制請見 [`docs/RESULTS.md`](docs/RESULTS.md)。

## Important Reproducibility Note

本 repository 是**框架公開參考實作**，不是所有論文歷史實驗環境的逐位元封存映像。

部分階段使用本地大型語言模型，因此生成內容可能受到模型版本、Ollama／Python／Docker 版本、推論設定與硬體環境影響。論文數據應視為原始實驗環境所得的 reference results，不應將特定數字視為所有未來執行都必須完全相同的 regression target。

詳細說明請見 [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md)。

## Requirements

- Python 3.11.15（論文正式實驗版本；3.11.x 為此 reference implementation 的目標相容範圍）
- Conda（建議）
- Ollama
- Docker Desktop / Docker Engine
- Python packages：`ollama`、`mcp`

### 論文正式模型設定

本公開版的預設模型設定與論文正式實驗一致：

| 角色 | 模型 | 用途 |
|---|---|---|
| Model A | `gemma4:31b` | Stage 1 意圖規格建立、Stage 2 程式生成、Stage 5 verification profile / MR 語意輔助、Stage 7 程式修復 |
| Model B | `nemotron-cascade-2:30b` | Stage 1 獨立需求一致性審查；對應論文中的 **Nemotron-Cascade-2-30B-A3B** |
| Embedding | `bge-m3` | Stage 1 向量語意相似度輔助檢查 |

除 Stage 1 的獨立審查模型與 embedding 模型外，本 reference implementation 中會直接呼叫 LLM 的主要生成／語意推論角色均以 `gemma4:31b` 為預設。Stage 3 行為抽取、Stage 4 行為對齊與 Stage 6 缺陷證據整理在目前公開版中主要採 AST、規則與既有 runtime evidence 處理。Stage 1 的 Model A 與 Model B 必須使用不同模型，以維持生成與獨立審查角色分離。

## Installation

建立 Conda 環境：

```powershell
conda env create -f environment.yml
conda activate thesis-framework
```

或僅安裝 Python dependencies：

```powershell
python -m pip install -r requirements.txt
```

準備 Docker sandbox image：

```powershell
docker pull python:3.11-slim
```

準備 Ollama 模型：

```powershell
ollama pull gemma4:31b
ollama pull nemotron-cascade-2:30b
ollama pull bge-m3
```

執行環境與 repository 完整性檢查：

```powershell
python preflight.py
python verify_release.py
```

`preflight.py` 只檢查 Python packages、Docker、Ollama 服務與模型，不會啟動論文流程。

## Configuration

Ollama 預設 API 位址為 `http://localhost:11434`。可使用以下環境變數覆寫：

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Stage 1 Ollama base URL |
| `OLLAMA_CHAT_URL` | `${OLLAMA_BASE_URL}/api/chat` | Stage 1 / Stage 5 / Stage 7 chat endpoint |
| `OLLAMA_EMBED_URL` | `${OLLAMA_BASE_URL}/api/embed` | embedding endpoint |
| `OLLAMA_EMBEDDINGS_URL` | `${OLLAMA_BASE_URL}/api/embeddings` | legacy embedding endpoint |
| `STRUCTURING_MODEL` | `gemma4:31b` | Stage 1 Model A：意圖規格建立 |
| `REVIEW_MODEL` | `nemotron-cascade-2:30b` | Stage 1 Model B：獨立需求一致性審查（Nemotron-Cascade-2-30B-A3B） |
| `EMBEDDING_MODEL` | `bge-m3` | Stage 1 embedding model |
| `GENERATION_MODEL` | `gemma4:31b` | Stage 2 程式生成 |
| `REPAIR_MODEL` | `gemma4:31b` | Stage 7 程式修復 |

Runtime 路徑以 repository 根目錄為基準。`--requirement-file` 使用相對路徑時，也會相對於 repository 根目錄解析。

## Quick Start

完整 Stage 1–7：

```powershell
python main.py --requirement-file examples/minimal_requirement.txt --mode repair-once
```

Repair loop：

```powershell
python main.py --requirement-file examples/minimal_requirement.txt --mode repair-loop --max-repairs 2 --generation-model gemma4:31b --mr-model gemma4:31b --repair-model gemma4:31b
```

對既有 RUN / GEN 重跑 Stage 3–7：

```powershell
python main_pipeline.py --run-id RUN-000001 --gen-id GEN-000001 --mode verify --mr-model gemma4:31b
```

`main.py` 完整執行後會保留 Stage 1 與 Stage 2 的 runtime 路徑，並額外建立集中快照，因此既有 RUN / GEN 仍可由 `main_pipeline.py` 重驗。

## Minimal Example

`examples/minimal_requirement.txt` 是不屬於正式資料集的最小 Counter 類別需求，用於第一次環境驗證。

完整流程會呼叫 Ollama 並啟動 Docker，因此不是純離線單元測試。

## Output

執行期間的工作資料：

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

完整流程結束後另建立集中快照：

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

主要正式判斷欄位位於 `verification_summary.json`：

```text
final_decision_for_delivery = verification_passed_for_current_scope
```

## Project Structure

```text
ai-agent-functional-verification/
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
└── README.md
```

## Repository Scope

為避免將評估資料與框架實作混在一起，本 repository **不包含**：

- CPE、APPS 正式資料集與隔離測試資料
- 歷史 `workspace` / `stage1_outputs`
- 原始逐題實驗輸出、log、cache
- Ollama 模型權重與 Docker image
- checkpoint、IDE / local environment 設定
- 論文 PDF 與簡報

論文數據僅以彙整 reference metrics 呈現。若要重新執行完整 benchmark，應自行準備具有合法使用權的資料與相同的隔離流程。

## Citation

若此 repository 用於研究或延伸實作，可使用 GitHub 的 **Cite this repository** 功能；引用資訊位於 [`CITATION.cff`](CITATION.cff)。

## License

目前 repository **未附開源授權條款**。公開可見不等同自動授予再散布或衍生使用權；如需進一步使用，請依適用的著作權規範處理。
