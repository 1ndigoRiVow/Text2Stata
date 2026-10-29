* 模板：缺失值多重插补（MI, chained equations）
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：多重插补不产出「一个」填好的数据集，它产出 m 个，用 Rubin 规则合并推断。
*   这里跑 m=5 的链式方程插补（MICE），并把「按行删除」与「MI」两套系数并排比较。
*   典型差别是 MI 的标准误更大——它把插补本身的不确定性算进去了；
*   均值插补 / 单次插补假装填补值就是真值，标准误偏小、t 值虚高。
*
*   前提假设是 MAR（缺失只与已观测到的变量有关）。如果缺失本身与研究的问题相关
*   （比如业绩差的企业不披露），这是 MNAR，插补修不了，要说清楚而不是假装补齐了。
*
*   插补后的数据只存本机，随结果回传的只有这两份报告。

local vars "$y $x $controls"
local cmp "$x $controls"

quietly count
local n_raw = r(N)

* ------------------------------------------------------------------
* 1. 缺失情况盘点 + 挑出要插补的变量
* ------------------------------------------------------------------
di "===== 缺失情况盘点 ====="

tempname fh
file open `fh' using "mi_report.csv", write text replace
file write `fh' "variable" "," "n_missing" "," "pct_missing" "," "to_impute" _n

local impvars ""
local preds ""
local n_any_missing 0

foreach v of local vars {
    capture confirm variable `v'
    if _rc continue

    capture confirm numeric variable `v'
    if _rc {
        di "  `v': 非数值变量，不参与插补"
        continue
    }

    quietly count if missing(`v')
    local nm = r(N)
    local pct = 100 * `nm' / max(`n_raw', 1)
    local pcts : display %6.2f `pct'
    di "  `v': 缺失 `nm' / `n_raw' (`pcts' %)"

    if `nm' > 0 {
        local impvars "`impvars' `v'"
        local n_any_missing = `n_any_missing' + `nm'
        file write `fh' "`v'" "," (`nm') "," %6.2f (`pct') "," "yes" _n
    }
    else {
        * 完整变量留作插补模型的预测变量
        local preds "`preds' `v'"
        file write `fh' "`v'" "," (`nm') "," %6.2f (`pct') "," "no" _n
    }
}
file close `fh'

* ------------------------------------------------------------------
* 2. 分支：没有缺失就直接给基准回归，不做插补
* ------------------------------------------------------------------
if "`impvars'" == "" {
    di _n "===== 关键变量没有缺失，不需要插补 ====="
    di "  直接给出完整数据的基准回归："
    regress $y $x $controls, robust
    di _n "  数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
    save "cleaned_data.dta", replace
}
else {
    di _n "  共 `n_any_missing' 个缺失单元需要插补，涉及变量：`impvars'"

    * ------------------------------------------------------------------
    * 3. 方案一：按行删除（完整观测）
    * ------------------------------------------------------------------
    preserve
    gen __nmiss = 0
    foreach v of local vars {
        capture confirm variable `v'
        if !_rc {
            replace __nmiss = __nmiss + missing(`v')
        }
    }
    keep if __nmiss == 0
    drop __nmiss

    quietly count
    local n_cc = r(N)
    local n_drop = `n_raw' - `n_cc'
    local pct_drop = 100 * `n_drop' / max(`n_raw', 1)
    local pcts : display %6.2f `pct_drop'

    di _n "===== 方案一：按行删除（完整观测）====="
    di "  样本量：`n_raw' → `n_cc'（删除 `n_drop' 行，`pcts' %）"
    regress $y $x $controls, robust
    est store cc_ols
    restore

    * ------------------------------------------------------------------
    * 4. 方案二：多重插补（m = 5）
    * ------------------------------------------------------------------
    di _n "===== 方案二：多重插补（m = 5, chained equations）====="

    preserve
    mi set mlong
    mi register imputed `impvars'

    if "`preds'" != "" {
        capture mi impute chained (regress) `impvars' = `preds', add(5) rseed(20260101)
    }
    else {
        capture mi impute chained (regress) `impvars', add(5) rseed(20260101)
    }
    local rc_imp = _rc

    if `rc_imp' {
        di "  ⚠ 插补失败（r(`rc_imp')）。"
        di "     常见原因：某个变量缺失比例过高、变量之间完全共线、或可用样本太少。"
        di "     这种情况下只能回退到按行删除，别硬填。"

        local mi_b ""
        local mi_se ""
        foreach v of local cmp {
            local mi_b "`mi_b' ."
            local mi_se "`mi_se' ."
        }
    }
    else {
        * 必须带 post：mi estimate 默认只把结果打在日志里，不写进 e()，
        * 不带 post 的话后面 _b[] / _se[] 一律取不到（实测 rc=111）。
        mi estimate, dots post: regress $y $x $controls, robust

        * 系数要在 e() 被覆盖之前抓出来
        local mi_b ""
        local mi_se ""
        foreach v of local cmp {
            local bb .
            local ss .
            capture local bb = _b[`v']
            capture local ss = _se[`v']
            local mi_b "`mi_b' `bb'"
            local mi_se "`mi_se' `ss'"
        }

        di _n "  插补后的数据（含 5 套插补）存为 mi_data.dta —— 留在执行目录。"
        save "mi_data.dta", replace
    }
    restore

    * ------------------------------------------------------------------
    * 5. 两套系数并排比较
    * ------------------------------------------------------------------
    est restore cc_ols
    local cc_b ""
    local cc_se ""
    foreach v of local cmp {
        local bb .
        local ss .
        capture local bb = _b[`v']
        capture local ss = _se[`v']
        local cc_b "`cc_b' `bb'"
        local cc_se "`cc_se' `ss'"
    }

    tempname gh
    file open `gh' using "mi_compare.csv", write text replace
    file write `gh' "variable" "," "b_complete_case" "," "se_complete_case" "," "b_mi" "," "se_mi" "," "se_change_pct" _n

    local i 0
    foreach v of local cmp {
        local ++i
        local b1 : word `i' of `cc_b'
        local s1 : word `i' of `cc_se'
        local b2 : word `i' of `mi_b'
        local s2 : word `i' of `mi_se'

        if "`b1'" != "." & "`s1'" != "." & "`b2'" != "." & "`s2'" != "." {
            local chg = 100 * (`s2' - `s1') / max(`s1', 1e-12)
            file write `gh' "`v'" "," %12.6f (`b1') "," %12.6f (`s1') "," %12.6f (`b2') "," %12.6f (`s2') "," %8.2f (`chg') _n
            local chgs : display %6.1f `chg'
            di "  `v': 系数 `b1' → `b2'，标准误 `s1' → `s2'（`chgs' %）"
        }
        else {
            file write `gh' "`v'" "," "`b1'" "," "`s1'" "," "`b2'" "," "`s2'" "," "na" _n
        }
    }
    file close `gh'

    di _n "  对比表已写入 mi_compare.csv。"
    di "  标准误往哪边走取决于两种力量的对比，别预设："
    di "    · MI 把删掉的那些行救回来了        → 精度上升；"
    di "    · MI 把「填进去的值不确定」计入了  → 精度下降。"
    di "  哪边占优取决于缺失比例与变量间相关性，以表里 se_change_pct 的实际符号为准。"

    if `pct_drop' > 20 {
        di _n "  ⚠ 按行删除会丢掉 `pcts' % 的样本，这个比例下删行本身就是有偏的，"
        di "     更应该用 MI；但同时也要问一句：为什么缺这么多。"
    }

    * ------------------------------------------------------------------
    * 6. 保存
    * ------------------------------------------------------------------
    di _n "===== 保存 ====="
    di "  cleaned_data.dta = 按行删除（完整观测）版本 —— 留在执行目录，不随结果回传。"
    di "  ⚠ MI 不产出单一数据集。要正式推断请用 mi_data.dta 跑 mi estimate，"
    di "     不要拿任何单次插补出来的数据跑回归——那会把标准误算小。"

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
}
