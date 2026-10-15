* 模板：计数模型（Poisson vs 负二项）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：$y 是非负整数计数变量（专利数、事故数、就诊次数…）。
* 两个模型都跑，用 lnalpha 与 AIC/BIC 判断是否存在过度分散：
* 过度分散存在时 Poisson 的标准误会低估，应以负二项为准。

capture which esttab
if _rc {
    ssc install estout, replace
}

di "===== 计数因变量分布 ====="
summarize $y, detail

di "===== Poisson 回归 ====="
poisson $y $x $controls, vce(robust)
est store cnt_poisson
di "----- Poisson 信息准则 -----"
estat ic

di "===== 负二项回归 ====="
nbreg $y $x $controls, vce(robust)
est store cnt_nbreg
di "----- 负二项信息准则 -----"
estat ic

di "判读：负二项结果里的 lnalpha 显著异于 0，说明存在过度分散，"
di "以负二项为准；AIC/BIC 更低的那一个更优。"

di "===== 边际效应（负二项）====="
margins, dydx($x $controls)

esttab cnt_poisson cnt_nbreg using "count_model_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N ll, labels("Observations" "Log likelihood")) ///
    mtitles("Poisson" "Neg. binomial") ///
    title("Count models: Poisson vs negative binomial")
