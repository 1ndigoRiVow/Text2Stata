* 模板：异常值识别与截尾处理
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：
*   两种识别口径同时给：3σ（假设近似正态）与 1.5×IQR（不假设分布，更稳健）。
*   默认按 1.5×IQR 截尾——把落在 [Q1-1.5·IQR, Q3+1.5·IQR] 之外的观测**整行删除**。
*   与 winsorize 模板的分工：winsorize 是把极值拉回边界（保留样本），
*   这里是直接删掉（丢样本）。样本本来就少时应该用 winsorize 而不是这个。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。

capture which esttab
if _rc {
    ssc install estout, replace
}

local vars "$y $x $controls"

* ------------------------------------------------------------------
* 1. 两种口径各识别到多少个异常值
* ------------------------------------------------------------------
di "===== 异常值识别 ====="

tempname fh
file open `fh' using "outlier_report.csv", write text replace
file write `fh' "variable" "," "n_3sigma" "," "pct_3sigma" "," "n_iqr" "," "pct_iqr" "," "lower_fence" "," "upper_fence" _n

local total_iqr 0
foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    quietly count if !missing(`v')
    local nvalid = r(N)
    if `nvalid' == 0 continue

    quietly summarize `v', detail
    local mean = r(mean)
    local sd = r(sd)
    local p25 = r(p25)
    local p75 = r(p75)
    local iqr = `p75' - `p25'
    local lo = `p25' - 1.5 * `iqr'
    local hi = `p75' + 1.5 * `iqr'
    local los : display %10.3f `lo'
    local his : display %10.3f `hi'

    * 3σ：正态假设下的口径
    if missing(`sd') | `sd' == 0 {
        local n_3sd 0
    }
    else {
        quietly count if !missing(`v') & abs(`v' - `mean') > 3 * `sd'
        local n_3sd = r(N)
    }

    * 1.5×IQR：不假设分布
    quietly count if !missing(`v') & (`v' < `lo' | `v' > `hi')
    local n_iqr = r(N)
    local total_iqr = `total_iqr' + `n_iqr'

    local p3 = 100 * `n_3sd' / max(`nvalid', 1)
    local pi = 100 * `n_iqr' / max(`nvalid', 1)

    di "  `v': 3σ 法 `n_3sd' 个 / IQR 法 `n_iqr' 个（截尾区间 [`los', `his']）"

    file write `fh' "`v'" "," (`n_3sd') "," %6.2f (`p3') "," (`n_iqr') "," %6.2f (`pi') "," %12.3f (`lo') "," %12.3f (`hi') _n
}
file close `fh'

* ------------------------------------------------------------------
* 2. 截尾前的基准
* ------------------------------------------------------------------
quietly count
local n_raw = r(N)

di _n "===== 截尾前 ====="
summarize $y $x

* ------------------------------------------------------------------
* 3. 按 1.5×IQR 截尾：越界的值置为缺失，再整行删除
* ------------------------------------------------------------------
foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    quietly summarize `v', detail
    local p25 = r(p25)
    local p75 = r(p75)
    local iqr = `p75' - `p25'
    local lo = `p25' - 1.5 * `iqr'
    local hi = `p75' + 1.5 * `iqr'

    replace `v' = . if !missing(`v') & (`v' < `lo' | `v' > `hi')
}

capture drop __anyout
gen __anyout = 0
foreach v of local vars {
    capture confirm variable `v'
    if !_rc {
        replace __anyout = __anyout + missing(`v')
    }
}
keep if __anyout == 0
drop __anyout

quietly count
local n_trim = r(N)
local n_dropped = `n_raw' - `n_trim'
local pct_drop = 100 * `n_dropped' / max(`n_raw', 1)
local pcts : display %6.2f `pct_drop'

di _n "===== 截尾后 ====="
di "  样本量：`n_raw' → `n_trim'（删除 `n_dropped' 行，`pcts' %）"
summarize $y $x

if `n_dropped' > 0 & `pct_drop' > 5 {
    di _n "  ⚠ 截尾删掉了 `pcts' % 的样本，比例偏高。"
    di "     先确认这些是录入错误还是真实极端值——真实极端值不该删。"
    di "     样本本来就少的话，改用 winsorize（把极值拉回边界，不丢样本）。"
}

* ------------------------------------------------------------------
* 4. 用截尾后的数据重跑基准回归
* ------------------------------------------------------------------
di _n "===== 截尾后基准回归 ====="
regress $y $x $controls, robust
est store trimmed_ols

esttab trimmed_ols using "outlier_trim_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Regression after trimming outliers (1.5 x IQR rule)")

di _n "  清洗后数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
save "cleaned_data.dta", replace
