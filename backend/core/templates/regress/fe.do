* 模板：固定效应
* 执行回归并输出稳健标准误

* 依赖守卫：缺少 estout 时自动安装（提供 esttab）
capture which esttab
if _rc {
    ssc install estout, replace
}

xtreg $y $x $controls, fe robust
est store model_fe
esttab model_fe using "fe_result.rtf", replace
