* 模板：面板平衡化
* 必需 global: $id $time
* 可选 global: $y $x $controls
*
* 约定：两种口径都给，让用户自己选。
*   方案 A「补齐」：tsfill 把每个个体的时间缺口补齐，再用个体内前后向填充补关键变量。
*     保住全部个体，代价是补出来的期是插出来的，不是真实观测。
*   方案 B「强平衡」：只保留每一期都有的个体。保住真实性，代价是丢样本。
*
*   给固定效应回归用的话，方案 B 更干净；做事件研究/平行趋势，方案 A 更稳。
*   默认保存方案 B（强平衡）为 cleaned_data.dta。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。

local vars "$y $x $controls"

* ------------------------------------------------------------------
* 1. 当前面板结构
* ------------------------------------------------------------------
* xtset 需要个体-时间组合唯一。有重复观测时会失败——
* 这时不能硬往下走，tsfill / xtdescribe 都依赖它，但方案 B 用 bysort 就能做。
capture xtset $id $time
local can_xtset = !_rc

if !`can_xtset' {
    di "⚠ 无法设定面板：$id 与 $time 的组合不唯一（存在重复观测）。"
    di "  方案 A（tsfill 补齐）需要这个前提，本次跳过。"
    di "  请先跑一次「重复观测处理」，或把数据 collapse 到个体-时间层面。"
}

di "===== 当前面板结构 ====="
if `can_xtset' {
    xtdescribe
}

quietly count
local n_raw = r(N)

capture drop __first
bysort $id ($time): gen __first = (_n == 1)
quietly count if __first
local n_ids = r(N)

quietly tabulate $time
local n_periods = r(r)

di "  观测数 `n_raw'    个体数 `n_ids'    期数 `n_periods'"
di "  若是平衡面板，观测数应等于 个体数 × 期数 = " `n_ids' * `n_periods'

capture drop __nobs
bysort $id: gen __nobs = _N
quietly summarize __nobs
local minobs = r(min)
local maxobs = r(max)
local is_balanced = (`minobs' == `maxobs')
di "  每个个体的期数：最少 `minobs'，最多 `maxobs'"
if `is_balanced' {
    di "  → 当前已经是平衡面板。"
}
else {
    di "  → 当前是非平衡面板，需要处理。"
}

* ------------------------------------------------------------------
* 2. 方案 A：补齐时间缺口
* ------------------------------------------------------------------
di _n "===== 方案 A：tsfill 补齐 + 个体内填充 ====="

if !`can_xtset' {
    di "  跳过：$id-$time 组合有重复，无法 tsfill。"
    local n_filled = `n_raw'
}
else {
    preserve
    tsfill
    quietly count
    local n_filled = r(N)
    local n_added = `n_filled' - `n_raw'
    di "  补齐后观测数：`n_raw' → `n_filled'（新增 `n_added' 个空期）"

    foreach v of local vars {
        capture confirm variable `v'
        if _rc continue
        bysort $id ($time): replace `v' = `v'[_n-1] if missing(`v')
        bysort $id ($time): replace `v' = `v'[_n+1] if missing(`v')
    }
    di "  关键变量的缺口已用个体内前后值填充"

    if "`vars'" != "" {
        di "  填充后关键变量均值："
        summarize $y $x
    }
    restore
}

* ------------------------------------------------------------------
* 3. 方案 B：强平衡（只保留每一期都有的个体）
* ------------------------------------------------------------------
di _n "===== 方案 B：只保留完整个体（强平衡）====="

preserve
keep if __nobs == `maxobs'

quietly count
local n_bal = r(N)
quietly count if __first
local n_ids_kept = r(N)
local n_ids_dropped = `n_ids' - `n_ids_kept'
local pct_kept = 100 * `n_bal' / max(`n_raw', 1)
local pcts : display %6.2f `pct_kept'

di "  观测数：`n_raw' → `n_bal'（保留 `pcts' %）"
di "  个体数：`n_ids' → `n_ids_kept'（丢弃 `n_ids_dropped' 个不完整个体）"

if `n_ids_dropped' > 0 {
    di "  ⚠ 丢掉 `n_ids_dropped' 个个体。若这些个体的进入退出本身有系统性"
    di "    （例如经营不善退市），强平衡会引入样本选择偏差——这时改用方案 A。"
}

if "`vars'" != "" {
    di "  强平衡后关键变量均值："
    summarize $y $x
}
restore

* ------------------------------------------------------------------
* 4. 报告与保存
* ------------------------------------------------------------------
tempname fh
file open `fh' using "panel_balance_report.csv", write text replace
file write `fh' "metric" "," "value" _n
file write `fh' "obs_raw" "," (`n_raw') _n
file write `fh' "n_entities" "," (`n_ids') _n
file write `fh' "n_periods" "," (`n_periods') _n
file write `fh' "min_periods_per_entity" "," (`minobs') _n
file write `fh' "max_periods_per_entity" "," (`maxobs') _n
file write `fh' "obs_after_tsfill" "," (`n_filled') _n
file write `fh' "obs_after_strict_balance" "," (`n_bal') _n
file write `fh' "entities_dropped" "," (`n_ids_dropped') _n
file close `fh'

di _n "===== 保存清洗结果 ====="
di "  采用方案 B（强平衡）：cleaned_data.dta —— 留在执行目录，不随结果回传。"
di "  需要补齐版本的话，用上面方案 A 的代码。"

keep if __nobs == `maxobs'
capture drop __first __nobs

save "cleaned_data.dta", replace
