* Template: data quality report for selected variables
* Required globals: $y $x
* Optional globals: $controls $id $time

capture log close _all
log using "data_quality_report.txt", text replace

local corevars "$y $x $controls"

di "===== Dataset overview ====="
describe
count

di "===== Core variable missingness ====="
misstable summarize `corevars'

di "===== Core variable distribution ====="
foreach v of varlist `corevars' {
    di "----- `v' -----"
    capture confirm numeric variable `v'
    if !_rc {
        summarize `v', detail
    }
    else {
        tab `v', missing
    }
}

di "===== Duplicate checks ====="
duplicates report

capture confirm variable $id
if !_rc {
    di "===== Duplicate id-time checks when possible ====="
    capture confirm variable $time
    if !_rc {
        duplicates report $id $time
    }
}

log close
