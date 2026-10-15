* 模板：缩尾预处理（1% / 99%）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：生成 _w 后缀的缩尾变量，不覆盖原始变量；
* 输出缩尾前后的描述统计对比，用缩尾后变量重跑一次基准回归，
* 并另存一份 winsorized_data.dta。

capture which winsor2
if _rc {
    ssc install winsor2, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}

di "===== 缩尾前 ====="
summarize $y $x $controls

winsor2 $y $x $controls, cuts(1 99) suffix(_w)

local vars "$y $x $controls"
local wlist ""
foreach v of local vars {
    * 注意是 `v'_w，不是 `v'_w' —— 后者会多出一个单引号，把后面的变量名列表带坏
    capture confirm variable `v'_w
    if !_rc {
        local wlist "`wlist' `v'_w"
    }
}

di "===== 缩尾后：`wlist' ====="
summarize `wlist'

* 约定顺序：$y 是第一个，$x 是第二个，其余按控制变量处理
local yw : word 1 of `wlist'
local xw : word 2 of `wlist'
local cw ""
local i 0
foreach v of local wlist {
    local ++i
    if `i' > 2 {
        local cw "`cw' `v'"
    }
}

di "===== 用缩尾后变量重跑基准回归 ====="
di "Y = `yw'    X = `xw'    控制变量 = `cw'"
regress `yw' `xw' `cw', robust
est store winsorized_ols

esttab winsorized_ols using "winsorize_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Regression on winsorized variables (1% / 99%)")

save "winsorized_data.dta", replace
