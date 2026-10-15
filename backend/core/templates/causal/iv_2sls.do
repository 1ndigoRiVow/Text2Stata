* 模板：工具变量两阶段最小二乘（2SLS）
* 必需 global: $y $x $iv
* 可选 global: $controls $id $time
*
* 约定：
*   $x 是内生的核心解释变量；$iv 是它对应的工具变量（可多个，空格分隔）。
*   给了 $id 就聚类到个体层面，否则用异方差稳健标准误。
*   弱工具变量看第一阶段的 F；内生性与过度识别看 ivreg2 的诊断。

capture which ivreg2
if _rc {
    ssc install ivreg2, replace
}
capture which ranktest
if _rc {
    ssc install ranktest, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}

di "===== 2SLS 第一阶段：内生变量对工具变量 ====="
di "内生变量 $x    工具变量 $iv"
di "经验判据：第一阶段 F > 10 才算不是弱工具变量。"
regress $x $iv $controls, robust
est store iv_first_stage

di "===== 2SLS 第二阶段 ====="
if "$id" != "" {
    ivregress 2sls $y $controls ($x = $iv), vce(cluster $id)
    est store iv_2sls
    di "----- ivreg2 诊断（聚类到 $id）-----"
    ivreg2 $y $controls ($x = $iv), cluster($id) first endog($x)
}
else {
    ivregress 2sls $y $controls ($x = $iv), robust
    est store iv_2sls
    di "----- ivreg2 诊断（异方差稳健）-----"
    ivreg2 $y $controls ($x = $iv), robust first endog($x)
}

di "判读：endog 检验显著说明 $x 确实内生，用 2SLS 是必要的；"
di "工具变量多于内生变量时，看 Hansen J 不应被拒绝。"

esttab iv_first_stage iv_2sls using "iv_2sls_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    mtitles("First stage" "2SLS") ///
    title("Instrumental variables: two-stage least squares")
