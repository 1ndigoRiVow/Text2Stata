* Template: first-difference panel regression
* Required globals: $y $x $id $time
* Optional globals: $controls

capture which esttab
if _rc {
    ssc install estout, replace
}

capture log close _all
log using "first_difference_diagnostics.txt", text replace

xtset $id $time

local rhs "$x $controls"
local diff_rhs ""

foreach v of varlist `rhs' {
    capture drop D_`v'
    gen double D_`v' = D.`v'
    local diff_rhs "`diff_rhs' D_`v'"
}

capture drop D_$y
gen double D_$y = D.$y

quietly regress D_$y `diff_rhs', vce(cluster $id)
est store first_difference

esttab first_difference using "first_difference_result.rtf", replace ///
    b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
    stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
    title("First-difference regression")

log close
