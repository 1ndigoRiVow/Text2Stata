* Component: winsorize selected variables at 1% and 99%
* Required globals: $y $x
* Optional globals: $controls

capture which winsor2
if _rc {
    ssc install winsor2, replace
}

local varlist "$y $x $controls"
winsor2 `varlist', cuts(1 99) replace
