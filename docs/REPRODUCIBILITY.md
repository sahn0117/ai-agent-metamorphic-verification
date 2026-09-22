# Reproducibility and Release Scope

## What this repository is

本 repository 提供的是論文框架的 **reference implementation**，目的是讓讀者可以理解並執行：

- intent structuring
- candidate generation
- behavior extraction / alignment
- verification profile construction
- rule-validated metamorphic execution
- defect feedback
- repair and re-verification

## What this repository is not

它不是論文所有歷史實驗的完整封存映像，也不承諾在不同時間、模型版本或硬體上重新執行後會得到逐題完全相同的生成物與 aggregate metrics。

## Why results can vary

框架部分階段依賴本地 LLM inference。即使需求文字相同，以下因素仍可能造成生成內容或後續路徑差異：

- Ollama / model build or digest
- inference configuration
- Python package versions
- Docker image revision
- hardware / driver / runtime differences
- candidate program generation and repair variation

因此，論文數據應視為**原始實驗環境的 archived reference results**。

## Two levels of reproducibility

### 1. Framework reproducibility

公開程式的主要目標是讓讀者能重新建立相同的研究工作流程與驗證機制：

```text
Requirement
→ Frozen Spec
→ Candidate Program
→ Behavior Evidence
→ MR Validation
→ Runtime Evidence
→ Repair Feedback
```

### 2. Experimental-number reproducibility

若要嚴格重現論文中的每一個 aggregate number，除了本 repository 之外，還需要與原實驗一致的：

- dataset snapshot
- preprocessing / filtering result
- blind-data isolation procedure
- model snapshot / digest
- runtime and dependency snapshot
- experiment orchestration settings
- independent-test data and sampling procedure

這些內容並未全部包含在公開 repository 中，因此不應把論文特定數值設成目前 `main` 分支的硬性 regression assertion。

## Evaluation-data isolation

論文正式實驗將題目範例輸入輸出於正式實驗前遮蔽，並將參考解、獨立測試資料與模型推論流程隔離；獨立測試只在 Baseline / Framework 各流程完成後用於後續評估。

公開 repository 也因此不把正式 CPE / APPS benchmark data、獨立測試資料或歷史逐題 workspace 與 framework code 混在一起。


## Thesis environment anchors

論文正式實驗記錄的主要環境包括 Windows 11、Python 3.11.15、Ollama、本地 Gemma 4 31B（Model A）、Nemotron-Cascade-2-30B-A3B（Model B）、bge-m3 與 NVIDIA GeForce RTX 5090。此 repository 使用可取得的 Ollama tag 與 `python:3.11-slim` 作為 reference defaults，但 mutable model/image tags 不等同論文當時的逐位元 snapshot；若要做嚴格重現，仍應另外保存 digest。

## Sample-I/O boundary in the reference implementation

論文 benchmark 在正式實驗前遮蔽題目範例輸入輸出。公開 reference implementation 為了讓使用者也能拿一般題目文字做功能驗證，保留了可選的 sample parser：若輸入需求仍含 sample input，經清理且可信度足夠的 sample input 可能被用作 generic MR 的 source seed；sample expected output 僅作為 basic sanity probe，不作為 formal MR 的 ground-truth oracle。若前處理已遮蔽 sample I/O，該 helper 自然不會取得相關內容。

## Original-I/O stability guardrail

目前公開參考實作對 `original_io_problem` 採取 registry-gated MR candidates：只有通過 statement evidence、已支援 relation type 與可執行性檢查的 MR 才能進入正式執行。

這個限制讓自由形式的 LLM 建議不會直接成為 formal oracle，並使正式 verdict 能回溯至明確的 relation checker 與 runtime evidence。


## Execution security scope

Stage 2 / Stage 5 使用 Docker 限制網路、capabilities、資源與程序數量，以降低候選程式對主機環境的直接影響。不過此機制的研究目的主要是**實驗隔離與執行一致性**，不是針對惡意程式碼設計的強敵對式 sandbox。Stage 5 的候選程式會在容器內實際執行，且當次 verification output directory 需以可寫方式掛載以保存 runtime report。若要驗證來源不可信或具攻擊意圖的第三方程式，應另外採用更強的 sandbox / VM / seccomp / filesystem isolation，而不要把本研究 Docker 設定視為完整安全邊界。

## Recommended use

若你要比較新的模型或新版本框架，建議：

1. 固定 dataset snapshot 與 blind-data protocol。
2. 記錄 model name / digest、Ollama、Python、Docker 與 GPU driver 版本。
3. 保留每個 RUN 的 `run_manifest.json` 與 `verification_summary.json`。
4. 將新結果標記為新的 experiment run，不覆蓋 thesis reference metrics。
5. 若修改驗證規則，另開 release / tag，避免與 thesis reference implementation 混淆。
