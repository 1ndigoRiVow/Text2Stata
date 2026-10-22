* 模板：重复观测处理
* 必需 global: $id
* 可选 global: $time $y $x $controls
*
* 约定：识别键给了 $time 就是「个体-时间」组合（面板数据的正确口径），
*   没给就按全部变量逐行比对。先报告再删，删之前把重复样本打出来看一眼。
*
*   重要：面板数据里「同一个体同一期出现两行」往往不是录入重复，
*   而是这份数据本身没聚合到个体-时间层面（例如一个企业一年有多条记录）。
*   那种情况该做的是 collapse 聚合，不是直接 drop——删除会静默丢信息。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。

* ------------------------------------------------------------------
* 1. 确定识别键
* ------------------------------------------------------------------
capture confirm variable $time
local has_time = !_rc

if `has_time' {
    local keys "$id $time"
    di "===== 重复观测检查（识别键：$id $time）====="
}
else {
    local keys ""
    di "===== 重复观测检查（识别键：全部变量）====="
    di "  ⚠ 未给出 $time，按整行比对。面板数据建议补上 $time。"
}

quietly count
local n_raw = r(N)
local n_after = `n_raw'

* ------------------------------------------------------------------
* 2. 报告
* ------------------------------------------------------------------
if "`keys'" != "" {
    duplicates report `keys'
    quietly duplicates tag `keys', generate(__dup)
}
else {
    duplicates report
    quietly duplicates tag, generate(__dup)
}

quietly count if __dup > 0
local n_duprows = r(N)
local pct_dup = 100 * `n_duprows' / max(`n_raw', 1)
local pcts : display %6.2f `pct_dup'

di "  涉及重复的行数：`n_duprows' / `n_raw'（`pcts' %）"

if `n_duprows' > 0 {
    di _n "===== 重复样本示例（前 10 组）====="
    if "`keys'" != "" {
        duplicates list `keys' in 1/200
    }
    else {
        duplicates list in 1/200
    }
}

* ------------------------------------------------------------------
* 3. 删除
* ------------------------------------------------------------------
if `n_duprows' > 0 {
    if "`keys'" != "" {
        duplicates drop `keys', force
    }
    else {
        duplicates drop, force
    }

    quietly count
    local n_after = r(N)
    local n_removed = `n_raw' - `n_after'
    di _n "===== 去重结果 ====="
    di "  样本量：`n_raw' → `n_after'（删除 `n_removed' 行）"

    if "`keys'" != "" {
        di "  去重后每个「`keys'」组合应唯一："
        isid $id $time
    }

    if `pct_dup' > 5 {
        di _n "  ⚠ 重复行占比 `pcts' %，偏高。"
        di "     面板数据里这通常意味着数据没聚合到个体-时间层面，"
        di "     正确做法是 collapse 聚合（例如对同一企业同一年取均值或加总），"
        di "     直接 drop 会静默丢掉信息。"
    }
}
else {
    di _n "  没有发现重复观测，数据无需处理。"
}

* ------------------------------------------------------------------
* 4. 报告与保存
* ------------------------------------------------------------------
tempname fh
file open `fh' using "deduplicate_report.csv", write text replace
file write `fh' "metric" "," "value" _n
file write `fh' "obs_raw" "," (`n_raw') _n
file write `fh' "obs_in_duplicate_groups" "," (`n_duprows') _n
file write `fh' "obs_after_drop" "," (`n_after') _n
file close `fh'

capture drop __dup

di _n "  去重后数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
save "cleaned_data.dta", replace
