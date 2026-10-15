* 模板：中介效应（两步法）
* 必需 global: $y $x $m
* 可选 global: $controls $id $time
*
* 方法论依据：江艇（2022）《因果推断经验研究中的中介效应与调节效应》。
* 走两步，不用 Baron-Kenny 那种逐步衰减判据，也不做 Sobel 检验：
*   第一步  X → M：估计核心解释变量对中介变量的效应，识别任务落在这一步。
*   第二步  M → Y（同时控制 X）：说明 M 与 Y 的相关性方向是否与理论一致。
* M 对 Y 的因果解释应当由理论论证来支撑，而不是由第二步的显著性来支撑，
* 所以这里不报告"中介效应占比"那类量——它要求 M → Y 也被识别，
* 而这个方法恰恰不主张把那一步当作因果识别。

capture which esttab
if _rc {
    ssc install estout, replace
}
capture which reghdfe
if _rc {
    ssc install ftools, replace
    ssc install reghdfe, replace
}

di "===== 中介效应（两步法）====="
di "X = $x     M = $m     Y = $y"

if "$id" != "" & "$time" != "" {
    di "面板设定：双向固定效应，标准误聚类到 $id"
    di "----- 第一步：X → M -----"
    reghdfe $m $x $controls, absorb($id $time) vce(cluster $id)
    est store med_step1

    di "----- 第二步：M → Y（控制 X）-----"
    reghdfe $y $m $x $controls, absorb($id $time) vce(cluster $id)
    est store med_step2
}
else {
    di "截面设定：异方差稳健标准误"
    di "----- 第一步：X → M -----"
    regress $m $x $controls, robust
    est store med_step1

    di "----- 第二步：M → Y（控制 X）-----"
    regress $y $m $x $controls, robust
    est store med_step2
}

di "判读：先看第一步 X 对 M 的效应是否稳健存在；"
di "再看第二步 M 的系数方向与理论预期是否一致。"
di "不做 Sobel 检验，也不算中介效应占比。"

esttab med_step1 med_step2 using "mediation_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    mtitles("Step 1: M" "Step 2: Y") ///
    title("Mediation: two-step (Jiang 2022)")
