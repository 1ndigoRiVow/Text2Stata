* 模板：分位数回归
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：在 0.25 / 0.5 / 0.75 三个分位点上估计，用 bootstrap 求标准误。
* 另检验核心解释变量的系数在两端分位数上是否相等——不相等才说明
* 影响在不同分布位置上存在异质性。

set seed 20261015

di "===== 分位数回归（Q25 / Q50 / Q75）====="
sqreg $y $x $controls, q(0.25 0.5 0.75) reps(200)
est store quantile_reg

di "===== 分位数间系数差异检验（Q25 vs Q75）====="
local xv : word 1 of $x
capture test [q25]`xv' = [q75]`xv'
if _rc {
    di "未能对该变量执行分位数间检验，请检查变量名。"
}

esttab quantile_reg using "quantile_reg_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N, labels("Observations")) ///
    title("Quantile regression (Q25 / Q50 / Q75)")
