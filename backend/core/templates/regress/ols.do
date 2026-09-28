* 模板：基础 OLS 回归
* 执行回归并输出稳健标准误

* 依赖守卫：缺少 estout 时自动安装（提供 esttab）
capture which esttab
if _rc {
    ssc install estout, replace
}

regress $y $x $controls, vce(robust)
est store model_ols
esttab model_ols using "ols_result.rtf", replace
