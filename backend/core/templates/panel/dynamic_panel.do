* 模板：动态面板 GMM（Arellano-Bond）
* 必需 global: $y $x $id $time
* 可选 global: $controls
*
* 约定：滞后一期被解释变量 L.$y 进入解释变量，两步差分 GMM。
*   AR(1) 应显著、AR(2) 不应显著（p > 0.1），Hansen 检验不应被拒绝（p > 0.1），
*   这三条是 GMM 结果能不能用的判据，都写在输出里。
* xtabond2 不支持因子变量语法，时间哑变量在这里手动生成。

capture which xtabond2
if _rc {
    ssc install xtabond2, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}

xtset $id $time

quietly levelsof $time, local(tvals)
local tdum ""
foreach t of local tvals {
    capture drop yr_`t'
    gen byte yr_`t' = ($time == `t')
    local tdum "`tdum' yr_`t'"
}

di "===== 动态面板 GMM（Arellano-Bond，两步）====="
di "工具变量：被解释变量的 2-4 阶滞后（gmm），其余变量视为外生（iv）。"

xtabond2 $y L.$y $x $controls `tdum', ///
    gmm($y, lag(2 4)) iv($x $controls `tdum') ///
    twostep robust small
est store gmm_ab

di "判读：AR(2) 与 Hansen 的 p 值都大于 0.1 时，模型设定才算站得住。"

esttab gmm_ab using "dynamic_panel_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N, labels("Observations")) ///
    title("Dynamic panel GMM (Arellano-Bond, two-step)")
