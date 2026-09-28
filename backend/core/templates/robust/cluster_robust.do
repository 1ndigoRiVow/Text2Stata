* Template: robust or cluster-robust OLS
* Required globals: $y $x
* Optional globals: $controls $id

capture which esttab
if _rc {
    ssc install estout, replace
}

capture log close _all
log using "cluster_robust_diagnostics.txt", text replace

capture confirm variable $id
if !_rc {
    di "===== OLS with standard errors clustered by $id ====="
    quietly regress $y $x $controls, vce(cluster $id)
    est store cluster_robust
}
else {
    di "===== $id is not available; falling back to heteroskedasticity-robust standard errors ====="
    quietly regress $y $x $controls, vce(robust)
    est store cluster_robust
}

esttab cluster_robust using "cluster_robust_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("Robust regression")

log close
