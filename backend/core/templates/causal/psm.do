* 模板：倾向得分匹配（PSM）
* 必需 global: $y $x
* 可选 global: $controls $id
*
* 约定：
*   $x 是 0/1 处理变量，$controls 是匹配用的协变量。
*   默认最近邻 1:1 无放回匹配，卡尺 0.05，限定共同支撑区间。
*   ATT 由 psmatch2 给出；另跑一列加权回归，方便和未匹配结果并排看。

capture which psmatch2
if _rc {
    ssc install psmatch2, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}

di "===== PSM：处理组分布 ====="
tab $x

di "----- 匹配前的简单对比（未调整差异）-----"
regress $y $x, robust
est store psm_unmatched

di "===== PSM：最近邻 1:1 匹配（卡尺 0.05，共同支撑）====="
psmatch2 $x $controls, outcome($y) logit ate neighbor(1) caliper(0.05) common
local att = r(att)
di "ATT = `att'"

capture confirm variable _weight
if !_rc {
    di "----- 匹配后（用 _weight 加权）-----"
    regress $y $x [pw=_weight] if _weight != ., robust
    est store psm_matched
}

di "===== 平衡性检验：匹配前后标准化偏差 ====="
di "判据：匹配后各协变量的标准化偏差一般应降到 10% 以内。"
capture noisily pstest $controls, both

esttab psm_unmatched psm_matched using "psm_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    mtitles("Unmatched" "Matched (ATT)") ///
    title("Propensity score matching: ATT = `att'")

capture noisily psgraph, bin(50)
capture noisily graph export "psm_balance.png", replace width(1800) height(1000)
