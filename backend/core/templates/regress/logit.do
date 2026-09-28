* Template: binary outcome Logit workflow
* Required globals: $y $x
* Optional globals: $controls

capture which esttab
if _rc {
    ssc install estout, replace
}

capture log close _all
log using "logit_diagnostics.txt", text replace

di "===== Logit model ====="
tab $y, missing

quietly logit $y $x $controls, vce(robust)
est store logit_model

esttab logit_model using "logit_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N ll chi2, labels("Observations" "Log likelihood" "Model chi-square")) ///
    title("Logit regression with robust standard errors")

di "===== Average marginal effects ====="
margins, dydx($x $controls)

predict double phat_logit if e(sample), pr
summarize phat_logit if e(sample), detail

log close
