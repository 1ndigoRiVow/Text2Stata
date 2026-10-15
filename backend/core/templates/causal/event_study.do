* 模板：事件研究法（平行趋势检验）
* 必需 global: $y $id $time $policy
* 可选 global: $controls $treat
*
* 约定：
*   $policy 是个体受政策影响的时期，可以写个体层面的变量名，
*   也可以直接写年份数字（如 2015）。
*   相对时间 event_time = $time - $policy，基期固定为 -1 期。
*   从未受处理个体的 event_time 为缺失，其各期哑变量恒为 0，
*   由个体与时间固定效应吸收。
*   期数超过正负 10 期的会被截掉，免得哑变量爆炸。

capture which reghdfe
if _rc {
    ssc install ftools, replace
    ssc install reghdfe, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}
capture which coefplot
if _rc {
    ssc install coefplot, replace
}

xtset $id $time

gen event_time = $time - $policy

quietly summarize event_time
local emin = r(min)
local emax = r(max)
if `emin' < -10 local emin = -10
if `emax' > 10 local emax = 10

local dlist ""
forvalues k = `emin'/`emax' {
    if `k' != -1 {
        local kname = cond(`k' < 0, "m" + string(abs(`k')), "p" + string(`k'))
        capture drop D_`kname'
        gen byte D_`kname' = (event_time == `k')
        local dlist "`dlist' D_`kname'"
    }
}

di "===== 事件研究法 ====="
di "基期：event_time = -1"
di "相对期哑变量：`dlist'"
di "平行趋势判据：政策前的各期系数应接近 0 且不显著。"

capture confirm variable $treat
if !_rc {
    di "----- 处理组分布 -----"
    tab $treat
}

reghdfe $y `dlist' $controls, absorb($id $time) vce(cluster $id)
est store event_study

esttab event_study using "event_study_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Event study: dynamic effects (base period = -1)")

capture noisily coefplot event_study, drop(_cons) vertical yline(0) ///
    title("Event study: dynamic effects (base = -1)") ///
    xtitle("Relative period") ytitle("Coefficient")
capture noisily graph export "event_study_plot.png", replace width(1800) height(1000)
