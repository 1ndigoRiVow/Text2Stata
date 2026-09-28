* Template: descriptive statistics for selected variables
* Required globals: $y $x
* Optional globals: $controls

capture log close _all
log using "descriptive_stats.txt", text replace

local varlist "$y $x $controls"

di "===== Selected variables ====="
di "`varlist'"

di "===== Missing-value overview ====="
misstable summarize `varlist'

di "===== Detailed summary statistics ====="
tabstat `varlist', statistics(n mean sd min p25 median p75 max) columns(statistics)

preserve
tempfile descstats
tempname handle
postfile `handle' str64 variable N mean sd min p25 p50 p75 max using `descstats', replace

foreach v of varlist `varlist' {
    quietly summarize `v', detail
    post `handle' ("`v'") (r(N)) (r(mean)) (r(sd)) (r(min)) (r(p25)) (r(p50)) (r(p75)) (r(max))
}

postclose `handle'
use `descstats', clear
export delimited using "descriptive_stats.csv", replace
restore

log close
