* Component: save a small reproducibility snapshot
* This file is intended to be copied into another template when needed.

notes: Text2Stata generated analysis snapshot
notes: Dependent variable = $y
notes: Explanatory variable = $x
notes: Controls = $controls

save "analysis_snapshot.dta", replace
