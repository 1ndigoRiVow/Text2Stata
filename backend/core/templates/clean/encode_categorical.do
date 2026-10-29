* 模板：分类 / 字符串变量编码
* 必需 global: $y $x
* 可选 global: $controls
*
* 约定：Stata 的回归不接受字符串变量，必须先转成带值标签的数值变量（encode），
*   或者直接用因子变量语法 i.<变量名>。这个模板自动扫描全表把这件事做完：
*     - 字符串变量        → encode 成 <原名>_code，保留文字值标签
*     - 取值数 ≤ 10 的    → 再生成一组 0/1 虚拟变量 <原名>_d<k>
*     - 取值数 11–50 的   → 只 encode，回归时用 i.<原名>_code（生成一堆 _d* 会把数据集撑大）
*     - 取值数 > 50 的    → 只 encode 并警告：这种一般是编号/名称，不是分类变量
*     - 低基数数值变量    → 只报告，不自动生成虚拟变量（用 i.<变量名> 更省事）
*
*   encode 按字母序赋码，所以因子变量的基准组是字母序最小的那一类。
*   要看某一码对应哪个类别，用 label list <变量名>_code。
*
*   编码后的数据只存本机，随结果回传的只有这份报告。

capture which esttab
if _rc {
    ssc install estout, replace
}

* ------------------------------------------------------------------
* 1. 扫描：哪些变量是分类变量
* ------------------------------------------------------------------
di "===== 分类 / 字符串变量扫描 ====="

ds, has(type string)
local strvars `r(varlist)'
local nstr : word count `strvars'

if `nstr' == 0 {
    di "  没有字符串变量。"
}
else {
    di "  字符串变量 `nstr' 个：`strvars'"
}

local lowcard ""
foreach v of varlist _all {
    capture confirm numeric variable `v'
    if _rc continue

    quietly levelsof `v', local(lv)
    local k : word count `lv'
    if `k' >= 2 & `k' <= 10 {
        local lowcard "`lowcard' `v'"
    }
}
local nlow : word count `lowcard'
if `nlow' > 0 {
    di _n "  低基数数值变量 `nlow' 个（取值数 2–10）：`lowcard'"
    di "  这些不额外生成虚拟变量，回归里直接写 i.<变量名> 即可。"
}

tempname fh
file open `fh' using "categorical_report.csv", write text replace
file write `fh' "variable" "," "type" "," "n_levels" "," "n_missing" "," "action" "," "example_levels" _n

* ------------------------------------------------------------------
* 2. 字符串变量：encode（+ 低基数时生成虚拟变量）
* ------------------------------------------------------------------
local encoded ""
local encoded_small ""

foreach v of local strvars {
    quietly levelsof `v', local(lv)
    local k : word count `lv'
    quietly count if missing(`v')
    local nmis = r(N)

    * 示例取值：取前 3 个，逗号会破坏 CSV，替换掉
    local ex ""
    local j 0
    foreach l of local lv {
        local ++j
        if `j' > 3 continue, break
        local l2 : subinstr local l "," " ", all
        local ex "`ex'`l2' / "
    }

    local newname "`v'_code"
    if length("`newname'") > 32 {
        di "  `v': 加 _code 后缀后变量名超过 32 字符，跳过编码"
        file write `fh' "`v'" "," "string" "," (`k') "," (`nmis') "," "skipped(name_too_long)" "," "`ex'" _n
        continue
    }

    capture confirm new variable `newname'
    if _rc {
        di "  `v': `newname' 已存在，沿用原有变量"
        file write `fh' "`v'" "," "string" "," (`k') "," (`nmis') "," "already_encoded" "," "`ex'" _n
        local encoded "`encoded' `newname'"
        if `k' <= 50 local encoded_small "`encoded_small' `newname'"
        continue
    }

    encode `v', gen(`newname')
    label variable `newname' "`v'（编码）"
    local encoded "`encoded' `newname'"

    if `k' > 50 {
        di "  `v': `k' 个取值 —— 已编码，但不生成虚拟变量（取值比样本还多就没意义了）"
        di "     ⚠ 这种一般是编号、名称或自由文本，通常不该当分类变量放进回归。"
        file write `fh' "`v'" "," "string" "," (`k') "," (`nmis') "," "encoded_only(too_many_levels)" "," "`ex'" _n
        continue
    }

    if `k' <= 50 local encoded_small "`encoded_small' `newname'"

    if `k' <= 10 {
        quietly levelsof `newname', local(codes)
        local nd 0
        foreach c of local codes {
            local dn "`v'_d`c'"
            capture confirm new variable `dn'
            if _rc continue

            local lbl "`c'"
            capture local lbl : label (`newname') `c'

            gen byte `dn' = (`newname' == `c') if !missing(`newname')
            label variable `dn' "`v' = `lbl'"
            local nd = `nd' + 1
        }
        di "  `v': `k' 个取值 —— 已编码为 `newname'，并生成 `nd' 个虚拟变量"
        file write `fh' "`v'" "," "string" "," (`k') "," (`nmis') "," "encoded+dummies(`nd')" "," "`ex'" _n
    }
    else {
        di "  `v': `k' 个取值 —— 已编码为 `newname'（回归里用 i.`newname'）"
        file write `fh' "`v'" "," "string" "," (`k') "," (`nmis') "," "encoded(use i.`newname')" "," "`ex'" _n
    }
}

* ------------------------------------------------------------------
* 3. 低基数数值变量：只登记到报告里
* ------------------------------------------------------------------
foreach v of local lowcard {
    quietly levelsof `v', local(lv)
    local k : word count `lv'
    quietly count if missing(`v')
    local nmis = r(N)

    local ex ""
    local j 0
    foreach l of local lv {
        local ++j
        if `j' > 3 continue, break
        local ex "`ex'`l' / "
    }
    file write `fh' "`v'" "," "numeric" "," (`k') "," (`nmis') "," "no_action(use i.`v')" "," "`ex'" _n
}
file close `fh'

* ------------------------------------------------------------------
* 4. 用编码后的变量重跑基准回归
* ------------------------------------------------------------------
di _n "===== 编码后基准回归 ====="

* 右侧变量里的字符串变量换成编码版，否则 regress 会直接报错
local rhs_all "$x $controls"
local rhs ""
foreach v of local rhs_all {
    capture confirm variable `v'
    if _rc continue

    capture confirm numeric variable `v'
    if !_rc {
        local rhs "`rhs' `v'"
    }
    else {
        capture confirm variable `v'_code
        if !_rc {
            local rhs "`rhs' `v'_code'"
            di "  控制变量 `v' 是字符串，改用编码版 `v'_code"
        }
        else {
            di "  ⚠ 控制变量 `v' 是字符串且未能编码，已从回归中剔除"
        }
    }
}

capture confirm numeric variable $y
local y_ok = !_rc

if !`y_ok' {
    di "  $y 不是数值变量，跳过基准回归（分类编码本身已完成）。"
}
else if "`rhs'" == "" {
    di "  没有可用的右侧变量，跳过基准回归。"
}
else {
    if "`encoded_small'" != "" {
        local first : word 1 of `encoded_small'
        di "  把 `first' 作为因子变量放入：regress $y `rhs' i.`first'"
        regress $y `rhs' i.`first', robust
    }
    else {
        regress $y `rhs', robust
    }
    est store cat_ols

    esttab cat_ols using "categorical_encode_result.rtf", replace ///
        b(%9.4f) se(%9.4f) star(* 0.10 ** 0.05 *** 0.01) ///
        stats(N r2_a, labels("Observations" "Adj. R-squared")) ///
        title("Regression with encoded categorical variables")
}

di _n "  含编码变量与虚拟变量的数据存为 cleaned_data.dta —— 留在执行目录，不随结果回传。"
save "cleaned_data.dta", replace
