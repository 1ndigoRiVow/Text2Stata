* Template: full OLS workflow with diagnostics
* Required globals: $y $x
* Optional globals: $controls

capture which esttab
if _rc {
    ssc install estout, replace
}

capture log close _all
log using "ols_full_diagnostics.txt", text replace

di "===== Full OLS workflow ====="
di "Dependent variable: $y"
di "Core explanatory variable: $x"
di "Controls: $controls"

quietly regress $y $x
est store ols_base

quietly regress $y $x $controls
est store ols_controls

quietly regress $y $x $controls, vce(robust)
est store ols_robust

esttab ols_base ols_controls ols_robust using "ols_full_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("OLS baseline and robust specifications")

di "===== Multicollinearity check: VIF ====="
quietly regress $y $x $controls
estat vif

di "===== Heteroskedasticity check: Breusch-Pagan / Cook-Weisberg ====="
estat hettest

predict double ols_fitted, xb
predict double ols_resid, resid

twoway ///
    (scatter $y ols_fitted, mcolor(navy%45) msymbol(circle_hollow)) ///
    (lfit $y ols_fitted, lcolor(maroon)), ///
    title("Observed vs fitted values") ///
    ytitle("Observed $y") ///
    xtitle("Fitted values")
graph export "ols_fit.png", replace width(1800)

log close
