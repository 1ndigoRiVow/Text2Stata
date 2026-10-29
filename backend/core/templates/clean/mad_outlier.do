* 模板：稳健异常值识别（MAD 口径）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：3σ 和 IQR 都会被异常值自己污染——均值和标准差本身就被极端值拉走了，
*   极端值越多的变量算出来的 σ 越大，反而越不容易被识别出来（掩蔽效应）。
*   MAD 用中位数，不受这个影响：
*       MAD = median(|x - median(x)|)
*       M_i = 0.6745 * (x_i - median) / MAD        （Iglewicz & Hoaglin 修正 z）
*   判 |M_i| > 3.5 为异常。0.6745 是让 M 在正态下与 σ 口径对齐的常数。
*
*   与另两个模板的分工：
*     - outlier_trim：IQR 截尾（依赖分位数，比 3σ 稳，但仍受分布形状影响）
*     - winsorize  ：缩尾，把极值拉回边界，不丢样本
*     - 本模板     ：MAD 识别 + 标记 + 全样本/剔除后两套回归对比，默认删行
*   样本本来就少时用 winsorize，不要用这个。
*
*   清洗后的数据只存本机，随结果回传的只有这份报告。

capture which esttab
if _rc {
    ssc install estout, replace
}

local vars "$y $x $controls"

quietly count
local n_raw = r(N)

capture drop __mad_flag
gen __mad_flag = 0

* ------------------------------------------------------------------
* 1. 逐变量算 MAD 与修正 z，同时和 3σ 口径对照
* ------------------------------------------------------------------
di "===== 稳健异常值识别（MAD 口径，阈值 |M| > 3.5）====="

tempname fh
file open `fh' using "mad_report.csv", write text replace
file write `fh' "variable" "," "median" "," "mad" "," "n_mad" "," "pct_mad" "," "n_3sigma" "," "pct_3sigma" "," "max_abs_M" _n

foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    capture confirm numeric variable `v'
    if _rc {
        di "  `v': 非数值变量，跳过"
        continue
    }

    quietly count if !missing(`v')
    local nvalid = r(N)
    if `nvalid' == 0 continue

    quietly summarize `v', detail
    local med = r(p50)
    local mean = r(mean)
    local sd = r(sd)

    * 3σ：正态假设下的口径，用来做对照
    if missing(`sd') | `sd' == 0 {
        local n_3sd 0
    }
    else {
        quietly count if !missing(`v') & abs(`v' - `mean') > 3 * `sd'
        local n_3sd = r(N)
    }

    * MAD
    capture drop __dev
    gen __dev = abs(`v' - `med')
    quietly summarize __dev, detail
    local mad = r(p50)
    drop __dev

    if missing(`mad') | `mad' == 0 {
        local p3 = 100 * `n_3sd' / max(`nvalid', 1)
        di "  `v': MAD = 0（取值高度集中，半数以上观测取同一个值），跳过稳健判定"
        file write `fh' "`v'" "," %12.4f (`med') "," %12.4f (`mad') "," "na" "," "na" "," (`n_3sd') "," %6.2f (`p3') "," "na" _n
        continue
    }

    capture drop __M
    gen __M = 0.6745 * (`v' - `med') / `mad'

    quietly count if !missing(__M) & abs(__M) > 3.5
    local n_mad = r(N)

    quietly summarize __M
    local maxM = max(abs(r(max)), abs(r(min)))

    replace __mad_flag = 1 if !missing(__M) & abs(__M) > 3.5
    drop __M

    local pm = 100 * `n_mad' / max(`nvalid', 1)
    local p3 = 100 * `n_3sd' / max(`nvalid', 1)
    local ms : display %8.2f `maxM'

    di "  `v': MAD 法 `n_mad' 个 / 3σ 法 `n_3sd' 个（最大 |M| = `ms'）"

    file write `fh' "`v'" "," %12.4f (`med') "," %12.4f (`mad') "," (`n_mad') "," %6.2f (`pm') "," (`n_3sd') "," %6.2f (`p3') "," %8.2f (`maxM') _n
}
file close `fh'

quietly count if __mad_flag
local n_flag = r(N)
local pct_flag = 100 * `n_flag' / max(`n_raw', 1)
local pcts : display %6.2f `pct_flag'

di _n "  合计标记 `n_flag' 行（占 `pcts' %）—— 只要任一关键变量被判异常就整行标记。"

if `n_flag' == 0 {
    di "  没有观测被 MAD 判为异常。这不等于没有极端值，"
    di "  只说明在「偏离中位数 3.5 个稳健标准差」这个口径下没有。"
}

* ------------------------------------------------------------------
* 2. 全样本基准（不剔除）
* ------------------------------------------------------------------
di _n "===== 全样本基准回归（未剔除）====="
regress $y $x $controls, robust
est store mad_full

* ------------------------------------------------------------------
* 3. 剔除异常行后重跑
* ------------------------------------------------------------------
preserve
keep if !__mad_flag
drop __mad_flag

quietly count
local n_kept = r(N)

di _n "===== 剔除异常后基准回归 ====="
di "  样本量：`n_raw' → `n_kept'（删除 `n_flag' 行，`pcts' %）"
regress $y $x $controls, robust
est store mad_trimmed

di _n "  清洗后数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
save "cleaned_data.dta", replace
restore

* ------------------------------------------------------------------
* 4. 两套结果并列输出
* ------------------------------------------------------------------
esttab mad_full mad_trimmed using "mad_outlier_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    mtitles("Full sample" "MAD-trimmed") ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Full sample vs MAD-trimmed (|M| > 3.5)")

di _n "  两列差异大 = 结论是被少数极端观测撑起来的，这种结论不能只报剔除后的版本。"
di "  差异不大 = 结果对异常值稳健，删不删都可以，那就别删（丢样本要付出代价）。"

if `pct_flag' > 5 {
    di _n "  ⚠ MAD 判掉了 `pcts' % 的样本，比例偏高。"
    di "     先确认这些是录入错误还是真实极端值——真实极端值是信息，不是噪声。"
    di "     删之前想清楚：你要的是「典型个体的关系」还是「全样本的关系」。"
}

capture drop __mad_flag
