* 模板：变量变换（对数 / 平方根 / 标准化 / 平方）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：不覆盖原始变量，一律生成后缀变量：_ln / _sqrt / _std / _sq。
*   取对数遇到非正值时，退化成 ln(1 + v - min)，并在日志里注明——
*   这样不会静默产生一堆缺失值。变换是否真的改善了分布，看偏度/峰度对比。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。

capture which esttab
if _rc {
    ssc install estout, replace
}

local vars "$y $x $controls"

* ------------------------------------------------------------------
* 1. 生成变换变量，同时记录偏度/峰度
* ------------------------------------------------------------------
di "===== 变量变换 ====="

tempname fh
file open `fh' using "transform_report.csv", write text replace
file write `fh' "variable" "," "skew_raw" "," "skew_ln" "," "skew_sqrt" "," "kurt_raw" "," "kurt_ln" _n

local ln_list ""
local std_list ""

foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    quietly summarize `v', detail
    local mn = r(min)
    local mean = r(mean)
    local sd = r(sd)
    local skew0 = r(skewness)
    local kurt0 = r(kurtosis)

    * --- 对数：非正值时退化成 ln(1 + v - min)，避免整列变缺失 ---
    if `mn' > 0 {
        gen `v'_ln = ln(`v')
        di "  `v'_ln = ln(`v')"
    }
    else {
        gen `v'_ln = ln(1 + `v' - `mn')
        di "  `v'_ln = ln(1 + `v' - `mn')  ⚠ 原变量含非正值（最小 `mn'），已平移后取对数"
    }

    * --- 平方根：负值同样先平移 ---
    if `mn' >= 0 {
        gen `v'_sqrt = sqrt(`v')
    }
    else {
        gen `v'_sqrt = sqrt(`v' - `mn')
    }

    * --- 标准化 ---
    if missing(`sd') | `sd' == 0 {
        gen `v'_std = 0
        di "  `v'_std：常数列，标准差为 0，无法标准化，置 0"
    }
    else {
        gen `v'_std = (`v' - `mean') / `sd'
    }

    * --- 平方（用于检验非线性 / U 型关系）---
    gen `v'_sq = `v' * `v'

    quietly summarize `v'_ln, detail
    local skew_ln = r(skewness)
    local kurt_ln = r(kurtosis)
    quietly summarize `v'_sqrt, detail
    local skew_sqrt = r(skewness)

    local s0 : display %7.3f `skew0'
    local sl : display %7.3f `skew_ln'
    di "    偏度：原 `s0' → 对数 `sl'"

    local ln_list "`ln_list' `v'_ln"
    local std_list "`std_list' `v'_std"

    file write `fh' "`v'" "," %8.3f (`skew0') "," %8.3f (`skew_ln') "," %8.3f (`skew_sqrt') "," %8.3f (`kurt0') "," %8.3f (`kurt_ln') _n
}
file close `fh'

* ------------------------------------------------------------------
* 2. 两种口径的基准回归
* ------------------------------------------------------------------
di _n "===== 基准回归：双对数（弹性口径）====="
local y_ln : word 1 of `ln_list'
local x_rest ""
local i 0
foreach v of local ln_list {
    local ++i
    if `i' > 1 {
        local x_rest "`x_rest' `v'"
    }
}
regress `y_ln' `x_rest', robust
est store log_log

di _n "===== 基准回归：标准化（可比系数口径）====="
local y_std : word 1 of `std_list'
local x_std ""
local i 0
foreach v of local std_list {
    local ++i
    if `i' > 1 {
        local x_std "`x_std' `v'"
    }
}
regress `y_std' `x_std', robust
est store standardized

esttab log_log standardized using "transform_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    mtitles("Log-log" "Standardized") ///
    title("Regression on transformed variables")

di _n "  双对数系数读弹性；标准化系数可比大小，但都只是相关口径，不是识别。"
di "  变换后数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
save "cleaned_data.dta", replace
