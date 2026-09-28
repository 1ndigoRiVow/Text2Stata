* 模板：固定效应
* 执行回归并输出稳健标准误
xtreg $y $x $controls, fe robust
est store model_fe
esttab model_fe using "fe_result.rtf", replace