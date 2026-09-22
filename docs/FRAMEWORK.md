# Framework Design

本文件說明公開參考實作中各階段的角色，以及 LLM 與 deterministic verification 的責任邊界。

## Design Principle

框架不將「模型認為程式正確」視為正式驗證結論。LLM 被用於需求理解、生成與修復等語意密集工作；正式功能驗證則要求可追溯的規則檢查與動態執行證據。

## Stage Responsibilities

| Stage | Main task | LLM role | Deterministic / executable role |
|---|---|---|---|
| 1. Intent Structuring | 將自然語言需求轉為固定驗收規格 | 建立候選 intent specification、語意審查 | schema / consistency / embedding 輔助檢查與 freeze |
| 2. Code Generation | 產生候選 Python 程式 | 主要生成者 | MCP tool boundary、檔案與介面限制 |
| 3. Behavior Extraction | 從候選程式取得行為證據 | 非必要 | AST 與原始碼規則分析 |
| 4. Behavior Alignment | 對齊需求與觀察行為 | 可提供語意資訊 | 結構化 evidence / alignment 規則 |
| 5. Verification Profile | 建立可驗證策略 | 一般 task family 可提供候選 profile | profile schema、task family、method roles、MR applicability 檢查 |
| 5. MR Execution | 建立 source / follow-up 並執行 | 不作最終裁判 | Docker execution + relation checking |
| 6. Defect Feedback | 將失敗轉為修復證據 | 非最終判定 | 由 runtime / MR evidence 結構化整理 |
| 7. Repair | 依缺陷證據修復 | 主要修復生成者 | 修復後重新跑驗證流程 |

## Original-I/O Tasks

`original_io_problem` 用於 CPE / UVA 類的標準輸入輸出題型，預期介面為：

```python
solve(input_text: str) -> str
```

目前公開參考實作採 **registry-gated MR candidates**：

1. 先建立保守的通用 I/O MR。
2. 語意型 MR 必須由題目敘述中出現足夠的 statement-level evidence 才能啟用。
3. 自由形式 LLM MR 建議不會直接覆蓋或繞過 registry gate。
4. MR 必須通過 profile / applicability / executability validation。
5. 通過後才建立 source / follow-up cases 並進入 Docker 執行。
6. formal verdict 由實際 runtime result 與 expected relation 決定。

目前程式內含的保守語意 evidence gates 例子包括 interval-order invariance、matrix zero-padding 與 skyline dominated-building relation。這些關係不是對所有 original-I/O 題目預設啟用，而是只有在題目敘述出現足夠需求證據時才建立候選。

## Basic Probes vs. Formal MR Evidence

基本 I/O probe 用於發現 import、執行例外、逾時、輸入解析或輸出格式等問題，但它不是正式 MR verdict 的替代品。若需求文字仍包含 sample input，reference implementation 可在通過清理與可信度檢查後，將該 input 僅作為部分 generic MR 的 source seed；sample expected output 仍只用於 basic probe，不作為 formal MR oracle。論文 benchmark 的 sample-I/O 遮蔽程序則以論文正式實驗設定為準。

formal metamorphic evidence 來自：

```text
source input
    ↓
candidate program
    ↓
source output

follow-up input
    ↓
candidate program
    ↓
follow-up output

source output + follow-up output
    ↓
expected MR relation
    ↓
PASS / VIOLATION / EXECUTION ERROR
```

因此，框架的核心不是讓 LLM 對結果做主觀打分，而是把 LLM 的語意能力限制在可被規則與執行證據檢查的工作流程中。
