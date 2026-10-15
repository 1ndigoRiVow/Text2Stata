* 模板：Tobit 模型（受限因变量）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：默认左截断点为 0，适用于金额、比例、时长这类下限归零的变量。
* 需要别的截断点时，改本模板的 ll() / ul() 选项。

capture which esttab
if _rc {
    ssc install estout, replace
}

di "===== Tobit：因变量分布 ====="
summarize $y, detail
quietly count if $y <= 0
local nzero = r(N)
di "因变量小于等于 0 的观测数：`nzero'（这部分是被截断的）"

tobit $y $x $controls, ll(0) vce(robust)
est store tobit_model

di "===== Tobit：边际效应 ====="
di "----- 对潜变量 y* 的边际效应 -----"
margins, dydx($x $controls) predict(ystar(0,.))
di "----- 对实际观测值 E[y | y > 0] 的边际效应 -----"
margins, dydx($x $controls) predict(e(0,.))

esttab tobit_model using "tobit_model_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N, labels("Observations")) ///
    title("Tobit regression (lower limit 0)")
