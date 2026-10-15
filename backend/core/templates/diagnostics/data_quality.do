* Template: data quality report for selected variables
* Required globals: $y $x
* Optional globals: $controls $id $time
*
* 输出说明：明细直接进主日志（结果校验层解析的是主日志），
* 逐变量汇总另存 data_quality_report.csv 供下载。
* 曾在这里用 log using 单独写 txt，结果把明细从主日志里劫走了，不要再加回来。

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

* ---------- 逐变量汇总导出 ----------
preserve
tempfile dqreport
tempname dqhandle
postfile `dqhandle' str64 variable N n_missing pct_missing mean sd min max using `dqreport', replace

foreach v of varlist `corevars' {
    quietly count
    local ntot = r(N)
    quietly count if missing(`v')
    local nmiss = r(N)
    local pct = 100 * `nmiss' / `ntot'

    local mn .
    local sd .
    local lo .
    local hi .
    capture confirm numeric variable `v'
    if !_rc {
        quietly summarize `v'
        local mn = r(mean)
        local sd = r(sd)
        local lo = r(min)
        local hi = r(max)
    }

    post `dqhandle' ("`v'") (`ntot') (`nmiss') (`pct') (`mn') (`sd') (`lo') (`hi')
}

postclose `dqhandle'
use `dqreport', clear
export delimited using "data_quality_report.csv", replace
restore
