* Template: pairwise correlation matrix
* Required globals: $y $x
* Optional globals: $controls

capture which esttab
if _rc {
    ssc install estout, replace
}

capture log close _all
log using "correlation_matrix.txt", text replace

local varlist "$y $x $controls"

di "===== Pairwise correlations with significance levels ====="
pwcorr `varlist', sig star(0.05) obs

quietly correlate `varlist'
matrix C = r(C)
esttab matrix(C, fmt(%9.4f)) using "correlation_matrix.rtf", replace ///
    title("Correlation matrix")

log close
