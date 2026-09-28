* 模板：基础 OLS 回归
* 执行回归并输出稳健标准误
regress $y $x $controls, vce(robust)
est store model_ols
esttab model_ols using "ols_result.rtf", replace
