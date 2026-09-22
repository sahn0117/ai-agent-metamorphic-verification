# Thesis Reference Results

本頁整理論文正式版本中報告的 aggregate results。這些數據是**論文原始實驗環境的 archived reference metrics**，不是對目前 repository 未來每次執行的固定輸出保證。

## 1. Primary Dataset: CPE

CPE 為論文主要實驗資料，共 1,012 題。

| Metric | Baseline | Framework |
|---|---:|---:|
| Final internal PASS | 726 / 1,012 (71.74%) | **968 / 1,012 (95.65%)** |
| Final internal non-PASS | 286 / 1,012 | **44 / 1,012** |

### CPE MR execution

| Metric | Framework |
|---|---:|
| Approved MRs | 3,027 |
| Executed MRs | 3,015 |
| Execution completion rate | 99.60% |
| Average executed MRs per problem | 2.98 |
| MR violations | 15 |
| MR violation ratio | 0.50% |

MR violation count 不等於錯誤題目數；同一題可能執行多個 MR，而 MR 通過亦不代表程式在所有有效輸入下均已被證明正確。

### CPE stratified independent evaluation

論文從 Baseline 與 Framework **各自的 final-PASS 母體**中分別分層抽取 200 題進行獨立測試。因此兩組樣本不保證為相同題號，也不是成對測試。

| Metric | Baseline | Framework |
|---|---:|---:|
| Independent-test PASS | 155 / 200 (77.50%) | **179 / 200 (89.50%)** |
| PASS but independent-test FAIL | 45 / 200 (22.50%) | **21 / 200 (10.50%)** |

論文亦記載這 400 個 CPE 抽樣案例均進行逐題人工複核。

## 2. Cross-dataset Evaluation: APPS Interview

APPS Interview 經篩選後使用 2,008 題，作為跨資料集評估。

| Metric | Baseline | Framework |
|---|---:|---:|
| Final internal PASS | 1,616 / 2,008 (80.48%) | **1,788 / 2,008 (89.04%)** |

### APPS independent tests on all final-PASS programs

| Metric | Baseline | Framework |
|---|---:|---:|
| Internal PASS population | 1,616 | 1,788 |
| PASS + independent-test PASS | 1,264 | **1,552** |
| PASS + independent-test FAIL | 352 | **236** |
| Independent-test pass proportion among internal PASS | 78.22% | **86.80%** |
| False-pass proportion among internal PASS | 21.78% | **13.20%** |

### APPS MR execution

| Metric | Framework |
|---|---:|
| Approved MRs | 4,541 |
| Executed MRs | 4,541 |
| Execution completion rate | 100.00% |
| Average executed MRs per problem | 2.26 |
| MR violations | 202 |
| MR violation ratio | 4.45% |

## 3. Interpretation Boundaries

這些數據應以**完整 Framework workflow 的系統層級結果**解讀，不應把差異直接歸因於單一 MR、單一 repair step 或單一 LLM call。

論文本身亦限制以下推論：

- internal PASS 不等同在所有有效輸入下完全正確；
- MR PASS 只表示已建立並執行的關係未被違反；
- CPE 獨立測試為兩組各自 PASS 母體的分層抽樣，不是相同題目的 paired comparison；
- APPS 則對各組所有 final-PASS 程式執行獨立測試；
- 不同模型版本、推論設定與執行環境可能影響重新生成結果。

因此，本頁數據適合作為論文結果的 reference summary，而不是目前公開程式的 bit-for-bit reproducibility target。
