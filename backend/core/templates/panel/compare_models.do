* Template: panel model comparison
* Required globals: $y $x $id $time
* Optional globals: $controls

capture which esttab
if _rc {
    ssc install estout, replace
}

capture which reghdfe
if _rc {
    ssc install reghdfe, replace
    ssc install ftools, replace
}

capture log close _all
log using "panel_compare_diagnostics.txt", text replace

xtset $id $time

quietly regress $y $x $controls, vce(cluster $id)
est store pooled_ols

quietly xtreg $y $x $controls, fe vce(cluster $id)
est store fe_oneway

quietly xtreg $y $x $controls, re vce(cluster $id)
est store re_model

quietly reghdfe $y $x $controls, absorb($id $time) vce(cluster $id)
est store twoway_fe

esttab pooled_ols fe_oneway re_model twoway_fe using "panel_compare_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    mtitles("Pooled OLS" "FE" "RE" "Two-way FE") ///
    title("Panel model comparison")

log close
