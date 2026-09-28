* 模板：双向固定效应
* 执行回归并输出聚类到个体层面的标准误

* 依赖守卫：缺少 ftools / reghdfe / estout 时自动安装
* 注意：reghdfe 依赖 ftools，必须先装 ftools
capture which reghdfe
if _rc {
    ssc install ftools, replace
    ssc install reghdfe, replace
}
capture which esttab
if _rc {
    ssc install estout, replace
}

reghdfe $y $x $controls, a($id $time) vce(cluster $id)
est store model_hdfe
esttab model_hdfe using "hdfe_result.rtf", replace
