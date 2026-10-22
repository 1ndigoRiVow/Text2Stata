* 模板：缺失值处理
* 必需 global: $y $x
* 可选 global: $controls $id $time
*
* 约定：
*   $y $x $controls 是要清洗的关键变量。
*   给了 $id $time 就多给一套"个体内前后向填充"的面板方案。
*   三种方案（按行删除 / 均值插补 / 面板填充）的样本量与均值对比都打在日志里，
*   默认保存"按行删除"版本为 cleaned_data.dta。
*
*   清洗后的数据只存本机，随结果回传的只有这份处理报告。
*   —— 数据不出本机是云端版的设计前提，别在这里开口子。

local vars "$y $x $controls"

* ------------------------------------------------------------------
* 1. 缺失情况盘点
* ------------------------------------------------------------------
di "===== 缺失情况盘点 ====="

tempname fh
file open `fh' using "missing_report.csv", write text replace
file write `fh' "variable" "," "n_missing" "," "pct_missing" _n

local total_n 0
foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    quietly count
    local tot = r(N)
    if `total_n' == 0 local total_n = `tot'

    quietly count if missing(`v')
    local nm = r(N)
    local pct = 100 * `nm' / max(`tot', 1)
    local pcts : display %6.2f `pct'

    di "  `v': 缺失 `nm' / `tot' (`pcts' %)"

    file write `fh' "`v'" "," (`nm') "," %6.2f (`pct') _n
}
file close `fh'

* ------------------------------------------------------------------
* 2. 方案一：按行删除（listwise deletion）
* ------------------------------------------------------------------
di _n "===== 方案一：按行删除任一关键变量缺失的观测 ====="

preserve
gen __nmiss = 0
foreach v of local vars {
    capture confirm variable `v'
    if !_rc {
        replace __nmiss = __nmiss + missing(`v')
    }
}
quietly count if __nmiss == 0
local n_listwise = r(N)
quietly count
local n_raw = r(N)
local n_dropped = `n_raw' - `n_listwise'
di "  样本量：`n_raw' → `n_listwise'（删除 `n_dropped' 行）"

if `n_listwise' > 0 {
    keep if __nmiss == 0
    drop __nmiss
    di "  删除后的关键变量均值："
    summarize $y $x
}
restore

* ------------------------------------------------------------------
* 3. 方案二：均值插补
* ------------------------------------------------------------------
di _n "===== 方案二：用变量均值插补 ====="
di "  ⚠ 均值插补会人为压低方差、削弱标准误，只适合缺失比例很低（< 5%）的情形。"

preserve
local filled ""
local n_imputed 0
foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    quietly count if missing(`v')
    local nm = r(N)
    if `nm' == 0 continue

    quietly summarize `v'
    local m = r(mean)
    replace `v' = `m' if missing(`v')
    local filled "`filled' `v'"
    local n_imputed = `n_imputed' + `nm'
}
quietly count
local n_mean = r(N)
di "  插补变量：`filled'"
di "  样本量：`n_raw' → `n_mean'（不丢样本，共填补 `n_imputed' 个缺失单元）"
di "  插补后的关键变量均值："
summarize $y $x
restore

* ------------------------------------------------------------------
* 4. 方案三：面板个体内前后向填充（给了 $id $time 才跑）
* ------------------------------------------------------------------
capture confirm variable $id
local has_id = !_rc
capture confirm variable $time
local has_time = !_rc

if `has_id' & `has_time' {
    di _n "===== 方案三：按个体前后向填充（面板） ====="

    preserve
    sort $id $time
    * 个体-时间组合有重复时 xtset 会失败，但前后向填充只靠 bysort，不依赖 xtset
    capture xtset $id $time

    foreach v of local vars {
        capture confirm variable `v'
        if _rc continue
        * 先向前填充，再向后填充（补齐个体首期为缺失的情形）
        bysort $id ($time): replace `v' = `v'[_n-1] if missing(`v')
        bysort $id ($time): replace `v' = `v'[_n+1] if missing(`v')
    }

    quietly count
    local n_panel = r(N)
    di "  样本量：`n_raw' → `n_panel'（补齐个体内缺口，不丢样本）"
    di "  填充后的关键变量均值："
    summarize $y $x
    restore
}
else {
    di _n "===== 方案三：跳过（未同时给出 $id 与 $time，无法按面板填充）====="
}

* ------------------------------------------------------------------
* 5. 保存：默认用按行删除的版本
* ------------------------------------------------------------------
di _n "===== 保存清洗结果 ====="
di "  采用方案一（按行删除）：cleaned_data.dta —— 留在执行目录，不随结果回传。"
di "  需要插补版本的话，把上面方案二/三的代码单独跑一遍即可。"

capture drop __nmiss
gen __nmiss = 0
foreach v of local vars {
    capture confirm variable `v'
    if !_rc {
        replace __nmiss = __nmiss + missing(`v')
    }
}
keep if __nmiss == 0
drop __nmiss

save "cleaned_data.dta", replace
