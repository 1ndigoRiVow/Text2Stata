* 模板：双向固定效应
* 执行回归并输出聚类到个体层面的标准误
reghdfe $y $x $controls, a($id $time) vce(cluster $id)
est store model_hdfe
esttab model_hdfe using "hdfe_result.rtf", replace