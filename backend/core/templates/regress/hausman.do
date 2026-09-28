xtreg $y $x $controls, re
outreg2 using "hausman", word ctitle(RE) dec(3) replace
est store re
xtreg $y $x $controls, fe
outreg2 using "hausman", word ctitle(FE) dec(3) append
est store fe
hausman fe re
outreg2 using "hausman", word ctitle(FE) dec(3) adds(Hausman, `r(chi2)', p-value, `r(p) ') append