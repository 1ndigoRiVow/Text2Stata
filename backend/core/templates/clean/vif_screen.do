* 模板：共线性诊断与变量筛选
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：VIF = 1 / (1 - R²_j)，R²_j 来自「用其余解释变量回归第 j 个变量」的辅助回归。
*   门槛取 10（对应辅助回归 R² = 0.9），逐步剔除当前 VIF 最高的变量，直到全部低于门槛。
*   剔除只按统计口径走，不做经济含义判断——被剔掉的变量可能恰恰是你关心的那个，
*   那种情况下别删，改成不把它和它的共线伙伴同时放进同一个模型。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。

capture which esttab
if _rc {
    ssc install estout, replace
}

local keep "$x $controls"
local dropped ""
local dropped_vif ""

* $x 是核心解释变量，不参与剔除。
* 共线性的处理对象是控制变量；核心变量和别人共线属于模型设定问题，
* 删掉它等于把要研究的东西删了——2026-10-22 实测：默认行为会把 $x 剔掉，
* 回归里连核心变量都没了，比共线本身严重得多。
local protected "$x"

* ------------------------------------------------------------------
* 1. 初始 VIF
* ------------------------------------------------------------------
local nk : word count `keep'
di "===== 共线性诊断 ====="
di "  待筛变量（`nk' 个）：`keep'"
di "  受保护（不参与剔除）：`protected'"

tempname fh
file open `fh' using "vif_report.csv", write text replace
file write `fh' "variable" "," "vif_initial" "," "status" _n

local vif_table ""
foreach v of local keep {
    local others : list keep - v
    if "`others'" == "" continue

    quietly regress `v' `others'
    local r2 = e(r2)
    if missing(`r2') {
        local vf .
    }
    else if `r2' > 0.9999 {
        local vf 99999
    }
    else {
        local vf = 1 / (1 - `r2')
    }
    local vfs : display %8.2f `vf'
    di "  `v': VIF = `vfs'"
    local vif_table "`vif_table' `v'"
}

* ------------------------------------------------------------------
* 2. 逐步剔除
* ------------------------------------------------------------------
di _n "===== 逐步剔除（门槛 VIF > 10）====="

local round 0
local done 0
while !`done' {
    local ++round
    if `round' > 30 {
        di "  已达 30 轮上限，停止。"
        local done 1
        continue, break
    }

    local nk : word count `keep'
    if `nk' <= 1 {
        di "  只剩 `nk' 个变量，无法继续筛。"
        local done 1
        continue, break
    }

    local worstv ""
    local worstvif 0
    foreach v of local keep {
        * 核心解释变量受保护，只看它的 VIF，绝不剔除
        local is_protected 0
        foreach p of local protected {
            if "`p'" == "`v'" {
                local is_protected = 1
            }
        }
        if `is_protected' continue

        local others : list keep - v
        if "`others'" == "" continue

        quietly regress `v' `others'
        local r2 = e(r2)
        if missing(`r2') continue
        if `r2' > 0.9999 {
            local vf 99999
        }
        else {
            local vf = 1 / (1 - `r2')
        }
        if `vf' > `worstvif' {
            local worstvif = `vf'
            local worstv "`v'"
        }
    }

    if `worstvif' > 10 {
        local wvs : display %8.2f `worstvif'
        di "  第 `round' 轮：剔除 `worstv'（VIF = `wvs'）"
        local keep : list keep - worstv
        local dropped "`dropped' `worstv'"
        local dropped_vif "`dropped_vif' `worstv'(`wvs')"
    }
    else {
        local done 1
    }
}

if "`dropped'" == "" {
    di "  没有变量的 VIF 超过 10，无需剔除。"
}
else {
    di "  剔除清单：`dropped'"
}
di "  保留清单：`keep'"

* ------------------------------------------------------------------
* 2b. 核心解释变量自身的共线性——只警告，不处理
* ------------------------------------------------------------------
foreach p of local protected {
    local others : list keep - p
    if "`others'" == "" continue

    quietly regress `p' `others'
    local r2 = e(r2)
    if missing(`r2') continue
    if `r2' > 0.9999 {
        local pvf 99999
    }
    else {
        local pvf = 1 / (1 - `r2')
    }
    local pvfs : display %8.2f `pvf'
    di "  核心解释变量 `p' 的 VIF = `pvfs'"

    if `pvf' > 10 {
        di _n "  ⚠ 核心解释变量 `p' 与其余变量高度共线（VIF = `pvfs'）。"
        di "     这个不靠删变量解决——删掉它就等于把要研究的东西删了。"
        di "     可选做法："
        di "       (1) 找出与它共线的那个控制变量，从模型里去掉那一个；"
        di "       (2) 换一个共线性较弱的变量来度量同一个构念；"
        di "       (3) 承认识别困难，在论文里说明，别假装解决了。"
    }
}

* ------------------------------------------------------------------
* 3. 写 VIF 表
* ------------------------------------------------------------------
foreach v of local vif_table {
    local others : list keep - v
    * 用初始变量集算初始 VIF 已算过，这里重新算一次以便统一输出
    local init_others ""
    foreach w of local vif_table {
        if "`w'" != "`v'" {
            local init_others "`init_others' `w'"
        }
    }

    if "`init_others'" == "" {
        local vf0 1
    }
    else {
        quietly regress `v' `init_others'
        local r2 = e(r2)
    }
    if missing(`r2') {
        local vf0 .
    }
    else if `r2' > 0.9999 {
        local vf0 99999
    }
    else {
        local vf0 = 1 / (1 - `r2')
    }

    local is_dropped 0
    foreach d of local dropped {
        if "`d'" == "`v'" {
            local is_dropped = 1
        }
    }
    if `is_dropped' {
        local status "dropped"
    }
    else {
        local status "kept"
    }

    file write `fh' "`v'" "," %8.2f (`vf0') "," "`status'" _n
}
file close `fh'

* ------------------------------------------------------------------
* 4. 用保留下来的变量重跑
* ------------------------------------------------------------------
di _n "===== 筛选后基准回归 ====="
regress $y `keep', robust
est store screened

esttab screened using "vif_screen_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Regression after VIF screening (threshold = 10)")

if "`dropped'" != "" {
    di _n "  ⚠ 被剔除的变量不等于不该控制，只是和保留变量信息重叠。"
    di "     若被剔除的恰是核心解释变量，说明它和其余变量高度共线——"
    di "     这时候要改的是模型设定，不是删变量。"
}

di _n "  保留变量的数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
keep $y `keep'
save "cleaned_data.dta", replace
