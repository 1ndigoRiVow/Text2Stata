* Template: basic DID when $x is the treatment-post interaction
* Required globals: $y $x $id $time
* Optional globals: $controls
*
* Convention:
*   $x should be a ready-to-use DID variable, such as treat_post.

capture which reghdfe
if _rc {
    ssc install reghdfe, replace
    ssc install ftools, replace
}

capture which esttab
if _rc {
    ssc install estout, replace
}

* 不要在这里 log using：那会把回归输出从主日志里劫走，
* 结果校验层读的是主日志，拿不到系数表就没法体检。
xtset $id $time

* 不要加 quietly：回归输出必须留在主日志里，结果校验层靠它做体检。
reghdfe $y $x $controls, absorb($id $time) vce(cluster $id)
est store did_basic

esttab did_basic using "did_basic_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Difference-in-differences regression")
