* Component: install common user-written packages if missing
* This file is intended to be copied into another template when needed.

capture which esttab
if _rc {
    ssc install estout, replace
}

capture which outreg2
if _rc {
    ssc install outreg2, replace
}

capture which reghdfe
if _rc {
    ssc install reghdfe, replace
    ssc install ftools, replace
}

capture which winsor2
if _rc {
    ssc install winsor2, replace
}
