* 模板：豪斯曼检验（RE vs FE）
* 同时输出随机效应与固定效应结果，并执行 Hausman 检验

* 依赖守卫：缺少 outreg2 时自动安装
* 注意：estout / reghdfe 不在此模板内使用，无需守卫
capture which outreg2
if _rc {
    ssc install outreg2, replace
}

xtreg $y $x $controls, re
outreg2 using "hausman", word ctitle(RE) dec(3) replace
est store re
xtreg $y $x $controls, fe
outreg2 using "hausman", word ctitle(FE) dec(3) append
est store fe
hausman fe re
outreg2 using "hausman", word ctitle(FE) dec(3) adds(Hausman, `r(chi2)', p-value, `r(p) ') append
