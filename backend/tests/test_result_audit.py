"""
结果校验层的回归测试（不需要真实 Stata，纯日志样本驱动）。

背景：`stata_worker` 只能回答"脚本跑通了吗"。2026 年的 CausalVerify 研究指出，
LLM 生成的计量代码里约 15.5% 能无错跑通、却给出完全错误的处理效应 —— 这类错误
执行判定拦不住。本模块把"结果像不像话"这件事独立出来做体检。

本测试锁死两件事：
  1. 该报的必须报（否则校验层形同虚设）；
  2. 不该报的绝不能报（误报是这层最大的死因，用户会教育自己忽略警告）。

运行方式（在 backend 目录下）：
    ../.venv/Scripts/python.exe tests/test_result_audit.py
"""
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.result_auditor import ResultAuditor  # noqa: E402

passed = failed = 0
auditor = ResultAuditor()


def check(name, got, expect):
    global passed, failed
    good = got == expect
    passed += good
    failed += (not good)
    print(f"{'PASS' if good else 'FAIL'}  {name}")
    if not good:
        print(f"      实际={got!r}  期望={expect!r}")


def codes(report):
    return sorted(item["code"] for item in report["warnings"])


# ----------------------------------------------------------------------
# 日志样本
# ----------------------------------------------------------------------

# 一份健康的 OLS 输出（sysuse auto 跑出来的真实形状）
LOG_HEALTHY_OLS = """
. regress price mpg weight, vce(robust)

Linear regression                               Number of obs     =         74
                                                F(2, 71)          =      13.75
                                                Prob > F          =     0.0000
                                                R-squared         =     0.2934
                                                Root MSE          =     2622.0

------------------------------------------------------------------------------
             |               Robust
       price |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
         mpg |  -49.51222   86.06261    -0.58   0.567    -221.1083    122.0838
      weight |   1.746559    .6413045     2.72   0.008     .4678204    3.025297
       _cons |   1946.069   3597.069     0.54   0.590    -5225.682    9117.821
------------------------------------------------------------------------------
"""

# 样本量为 0：跑通了，但什么都没估计出来
LOG_EMPTY_SAMPLE = """
. regress price notexistvar

Linear regression                               Number of obs     =          0
                                                F(0, 73)          =        .
                                                Prob > F          =        .
                                                R-squared         =        .
------------------------------------------------------------------------------
       price |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
------------------------------------------------------------------------------
"""

# 样本量极小
LOG_TINY_SAMPLE = """
. regress y x

Linear regression                               Number of obs     =          5
                                                R-squared         =     0.4120
------------------------------------------------------------------------------
           y |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
           x |   0.532106   .0912345     5.83   0.000     .3521064    .7121064
       _cons |   1.204411   .3200101     3.76   0.020     .5120014    1.896820
------------------------------------------------------------------------------
"""

# 量纲失控：一个变量以元计，另一个以万元计
LOG_HUGE_COEF = """
. regress gdp revenue

Linear regression                               Number of obs     =        280
                                                R-squared         =     0.8821
------------------------------------------------------------------------------
         gdp |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
     revenue |   48213.44   9134.201     5.28   0.000     30241.11     66185.77
       _cons |   102.4471     88.2014     1.16   0.247     -71.20413     276.0983
------------------------------------------------------------------------------
"""

# 严重多重共线
LOG_HIGH_VIF = """
. regress y x1 x2

Linear regression                               Number of obs     =        500
                                                R-squared         =     0.9102
------------------------------------------------------------------------------
           y |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
          x1 |   1.204411   .3200101     3.76   0.000     .5120014    1.896820
          x2 |   .8120041   .2210088     3.67   0.000     .3780014    1.246007
       _cons |   .4002134   .1100021     3.64   0.000     .1834013    .6170255
------------------------------------------------------------------------------

    Variable |       VIF       1/VIF
-------------+----------------------
          x1 |     14.52      0.068870
          x2 |     13.87      0.072098
-------------+----------------------
    Mean VIF |     14.20
"""

# 聚类数过少：脚本完全跑通，错误完全隐形
LOG_FEW_CLUSTERS = """
. reghdfe y x, absorb(id year) cluster(firm_id)

(MWFE estimator converged in 4 iterations)

             |               Robust
           y |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
           x |   .3321064   .1012345     3.28   0.002     .1312044    .5330084
       _cons |   .2014401   .0802144     2.51   0.014     .0420118    .3608684
------------------------------------------------------------------------------
Absorbed degrees of freedom:
-----------------------------------------------------+
 Absorbed FE | Categories  - Redundant  = Num. Coefs |
-------------+---------------------------------------|
          id |         8           0           8     |
        year |         6           1           5     |
-----------------------------------------------------+

Number of clusters (firm_id) = 8
"""

# FE 吸收掉不随时间变化的核心变量：这是固定效应的真实软肋
LOG_NO_WITHIN = """
. xtreg tfp focus lev, fe

Fixed-effects (within) regression               Number of obs     =       3200
Group variable: id                              Number of groups  =        400
                                                R-squared         =     0.1820
------------------------------------------------------------------------------
         tfp |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
       focus |   0.182004   .0412033     4.42   0.000     .1012044    .2628036
         lev |          0  (omitted)
       _cons |   1.204411   .3200101     3.76   0.000     .5120014    1.896820
------------------------------------------------------------------------------

note: lev omitted because of collinearity

Number of clusters (id) = 400
"""

# 正常但带提示：OLS 自动丢观测 + 变量共线剔除
LOG_DROPPED_OBS = """
. regress y x size

Linear regression                               Number of obs     =        412
                                                R-squared         =     0.3120
------------------------------------------------------------------------------
           y |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
           x |   0.432104   .0820101     5.27   0.000     .2710814    .5931266
        size |   0.120441   .0310021     3.88   0.000     .0594014    .1814806
       _cons |   1.204411   .3200101     3.76   0.000     .5120014    1.896820
------------------------------------------------------------------------------
(88 observations dropped)

note: size omitted because of collinearity

Number of clusters (id) = 402
"""

# 收敛失败
LOG_NO_CONVERGENCE = """
. logit y x

note: 12 failures and 0 successes completely determined.
Iteration 0:   log likelihood = -120.4482
Iteration 1:   log likelihood = -118.2014
convergence not achieved

Logistic regression                             Number of obs     =        240
                                                R-squared         =     0.1820
------------------------------------------------------------------------------
           y |      Coef.   Std. Err.      z    P>|z|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
           x |   2.204411   .6200101     3.56   0.000     1.012044    3.396780
       _cons |  -1.204411   .3200101    -3.76   0.000    -1.831620   -.5772014
------------------------------------------------------------------------------
"""

# 只跑了一次 summarize，没有回归表 —— 用于验证保守性
LOG_BARE_SUMMARY = """
. sysuse auto, clear
(1978 automobile data)

. summarize price mpg

    Variable |        Obs        Mean    Std. dev.       Min        Max
-------------+---------------------------------------------------------
       price |         74    6165.257    2949.496       3291      15906
         mpg |         74    21.29729    5.785503         12         41
"""

# descriptive_stats 链路：某变量全缺失
LOG_DESCRIPTIVE_EMPTY_VAR = """
. tabstat tfp focus emptyvar, statistics(n mean sd min max) columns(statistics)

    Variable |         N      Mean        SD       Min       Max
-------------+--------------------------------------------------
         tfp |      1200     .5234     .1123     .0812     1.2041
       focus |      1200     .4021     .1892     .0010     .9802
    emptyvar |         0         .         .         .         .
----------------------------------------------------------------
"""

# descriptive_stats 正常输出
LOG_DESCRIPTIVE_OK = """
. tabstat tfp focus lev, statistics(n mean sd min p25 median p75 max) columns(statistics)

    Variable |         N      Mean        SD       Min       p25    Median       p75       Max
-------------+----------------------------------------------------------------------------------
         tfp |      1200     .5234     .1123     .0812    .4401     .5204     .6012     1.2041
       focus |      1200     .4021     .1892     .0010     .2601     .4010     .5402     .9802
         lev |      1200     .4820     .2031     .0102     .3301     .4710     .6210     .9502
------------------------------------------------------------------------------------------------
"""


# ----------------------------------------------------------------------
# 真实日志样本（从 backend/workspace/ 的真实运行里逐字复制）
#
# 为什么必须有这一组：手写的样本会不自觉地"写得比现实更整齐"。
# 下面这些格式差异，全部只在真实 Stata 输出里出现，手写样本掩盖过一轮：
#   1. xtreg 的表头是 "Coefficient / std. err."（长写），regress 是 "Coef. / Std. Err."
#   2. Stata 大量用省略前导零的小数（.1909618 = 0.1909618），
#      而缺失值也是点号 —— 两者长得几乎一样
#   3. 变量名列后有竖线，列宽随变量名长度动态变化
# ----------------------------------------------------------------------

# 真实 xtreg, fe 输出（长写表头 + 前导点小数）
LOG_REAL_XTREG_FE = """
. xtreg $y $x $controls, fe robust

Fixed-effects (within) regression               Number of obs     =     63,207
Group variable: id                              Number of groups  =      5,376

R-squared:                                      Obs per group:
     Within  = 0.0016                                         min =          1
     Between = 0.0048                                         avg =       11.8
     Overall = 0.0032                                         max =         14

                                                F(2, 5375)        =      10.20
corr(u_i, Xb) = -0.1129                         Prob > F          =     0.0000

                                 (Std. err. adjusted for 5,376 clusters in id)
------------------------------------------------------------------------------
             |               Robust
         tfp | Coefficient  std. err.      t    P>|t|     [95% conf. interval]
-------------+----------------------------------------------------------------
       focus |  -.7964761   .1909618    -4.17   0.000    -1.170839   -.4221136
         lev |   .0708628   .0386465     1.83   0.067       -.0049    .1466257
       _cons |  -1.110861    .022412   -49.57   0.000    -1.154798   -1.066925
------------------------------------------------------------------------------
     sigma_u |  .73168393
     sigma_e |  .43942653
         rho |  .73492511   (fraction of variance due to u_i)
------------------------------------------------------------------------------
"""

# 真实 reghdfe 输出（聚类数写法带括号变量名）
LOG_REAL_REG_HDFE = """
. reghdfe $y $x $controls, a($id $time) vce(cluster $id)

HDFE Linear regression                            Number of obs   =     63,465
Absorbing 2 HDFE groups                           F(   1,   5372) =       3.48
                                                  Prob > F        =      0.062
                                                  R-squared       =     0.0006
Number of clusters (id)      =      5,373         Root MSE        =     0.4144

                                 (Std. err. adjusted for 5,373 clusters in id)
------------------------------------------------------------------------------
             |               Robust
         tfp |      Coef.   Std. Err.      t    P>|t|     [95% Conf. Interval]
-------------+----------------------------------------------------------------
       focus |  -.3316219   .1773184    -1.87   0.062    -.6792381     .015994
       _cons |  -1.120453   .0147816   -75.80   0.000    -1.149431   -1.091475
------------------------------------------------------------------------------
"""

# 真实 tabstat 输出 —— 注意这**不是**系数表，绝不能被当成系数行
LOG_REAL_TABSTAT = """
. tabstat tfp focus lev, statistics(n mean sd min p25 p50 p75 max) columns(statistics)

    Variable |         N      Mean        SD       Min       p25       p50       p75       Max
-------------+----------------------------------------------------------------------------------
         tfp |     63469 -1.148062  .8333764 -11.29969 -1.666546 -1.250989 -.7369822  5.807148
       focus |     76580 -.2233934   .181762 -1.282342 -.3379528 -.2139161 -.0935515  .064111
         lev |     66263  1.995214  .9099066         0         1  2.079442  2.890372  4.394449
------------------------------------------------------------------------------------------------
"""

# 真实 misstable 输出（同样是"变量 + 一堆数字"，也不是系数表）
LOG_REAL_MISSTABLE = """
. misstable summarize tfp focus lev

                                                               Obs<.
                                                +------------------------------
               |                                |            |----- Missing ----|
    Variable   |     Obs=.     Obs>.     Obs<.  | values        Min         Max
----------------+--------------------------------+------------------------------
         tfp   |    13,111              63,469  |   >500  -11.29969    5.807148
       focus   |     1,423              76,580  |   >500  -.6519507    1.106864
----------------+--------------------------------+------------------------------
"""


# ----------------------------------------------------------------------
def main():
    print("=" * 78)
    print("一、该报的必须报")
    print("=" * 78)

    report = auditor.audit("ols_basic", LOG_EMPTY_SAMPLE)
    check("样本量为 0 → empty_sample", "empty_sample" in codes(report), True)
    check("样本量为 0 → 致命级", report["warnings"][0]["level"], "high")

    report = auditor.audit("ols_basic", LOG_TINY_SAMPLE)
    check("5 个观测 → tiny_sample", codes(report), ["tiny_sample"])

    report = auditor.audit("ols_basic", LOG_HUGE_COEF)
    check("系数 48213 → huge_coefficient", codes(report), ["huge_coefficient"])

    report = auditor.audit("ols_full", LOG_HIGH_VIF)
    check("VIF 14.52 → high_vif + high_mean_vif",
          codes(report), ["high_mean_vif", "high_vif"])

    report = auditor.audit("hdfe", LOG_FEW_CLUSTERS)
    check("聚类数 8 → few_clusters", "few_clusters" in codes(report), True)
    check("聚类数 8 → 致命级", report["warnings"][0]["level"], "high")

    report = auditor.audit("fe", LOG_NO_WITHIN)
    check("核心变量被 FE 吸收 → no_within_variation",
          "no_within_variation" in codes(report), True)

    report = auditor.audit("logit_binary", LOG_NO_CONVERGENCE)
    check("迭代未收敛 → stata_convergence",
          "stata_convergence" in codes(report), True)

    report = auditor.audit("ols_basic", LOG_DROPPED_OBS)
    check("丢弃 88 个观测 → stata_dropped_obs",
          "stata_dropped_obs" in codes(report), True)

    report = auditor.audit("descriptive_stats", LOG_DESCRIPTIVE_EMPTY_VAR)
    check("描述统计里有全缺失变量 → all_missing_variable",
          "all_missing_variable" in codes(report), True)

    print()
    print("=" * 78)
    print("二、不该报的绝不能报（误报是这层最大的死因）")
    print("=" * 78)

    report = auditor.audit("ols_basic", LOG_HEALTHY_OLS)
    check("健康 OLS → 零警告", codes(report), [])
    check("健康 OLS → passed", report["status"], "passed")
    check("健康 OLS → 74 个观测不触发小样本",
          any("sample" in c for c in codes(report)), False)

    report = auditor.audit("descriptive_stats", LOG_DESCRIPTIVE_OK)
    check("健康的描述统计 → 零警告", codes(report), [])

    report = auditor.audit("hdfe", LOG_FEW_CLUSTERS)
    check("聚类数 400 的健康面板不会误报 few_clusters",
          auditor.audit("hdfe", LOG_HEALTHY_OLS)["warning_count"], 0)

    report = auditor.audit("ols_basic", LOG_DROPPED_OBS)
    check("'Number of obs' 不会被误认成系数行",
          "huge_coefficient" in codes(report), False)
    check("'F(2, 71)' 不会被误认成系数行",
          "invalid_stderr" in codes(report), False)

    report = auditor.audit("ols_basic", LOG_BARE_SUMMARY)
    check("只有 summarize、无回归表 → 不误报回归问题",
          codes(report), [])

    print()
    print("=" * 78)
    print("三、边界与健壮性")
    print("=" * 78)

    report = auditor.audit("ols_basic", "")
    check("空日志 → 不产生警告（执行层已判失败，不重复报）",
          (report["status"], report["warning_count"]), ("passed", 0))

    report = auditor.audit("ols_basic", None)
    check("日志为 None → 不抛异常",
          report["status"], "passed")

    report = auditor.audit("未知模板", LOG_HEALTHY_OLS)
    check("未知模板 → 只跑通用检查，不崩",
          report["status"], "passed")

    blob = LOG_DROPPED_OBS * 3
    report = auditor.audit("ols_basic", blob)
    dropped = [c for c in codes(report) if c == "stata_dropped_obs"]
    check("同一问题重复出现 → 只报一条", len(dropped), 1)

    report = auditor.audit("ols_basic", LOG_TINY_SAMPLE)
    levels = [w["level"] for w in report["warnings"]]
    check("警告按严重程度排序", levels, sorted(levels, key=lambda v: {"high": 0, "medium": 1, "low": 2}[v]))

    print()
    print("=" * 78)
    print("四、产物文件复核（磁盘级）")
    print("=" * 78)

    with tempfile.TemporaryDirectory(prefix="t2s_audit_") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "empty.csv").write_text("variable,N,mean\n", encoding="utf-8")
        # 整列全空（descriptive.do 里某个统计量没算出来时会长这样）
        (tmp_path / "blankcol.csv").write_text(
            "variable,N,mean\nfoo,12,\nbar,15,\n", encoding="utf-8")
        (tmp_path / "good.csv").write_text(
            "variable,N,mean\nfoo,12,1.2\nbar,15,2.4\n", encoding="utf-8")

        out = auditor.audit_artifacts(["empty.csv", "blankcol.csv", "good.csv"], tmp_path)
        found = sorted(item["code"] for item in out)
        check("空表 → csv_no_rows", "csv_no_rows" in found, True)
        check("空列 → csv_empty_column", "csv_empty_column" in found, True)
        check("健康 CSV → 不报", len([i for i in out if "good" in i["evidence"]]), 0)

        out = auditor.audit_artifacts([], tmp_path)
        check("空文件列表 → 不崩", out, [])
        out = auditor.audit_artifacts(["notexist.csv"], tmp_path)
        check("文件不存在 → 跳过不崩", out, [])

    print()
    print("=" * 78)
    print("五、真实日志格式（手写样本曾掩盖的坑）")
    print("=" * 78)

    # --- 5.1 省略前导零的小数：这不是缺失值 ---
    from core.result_auditor import _to_float
    check("'.1909618' 是小数不是缺失值", _to_float(".1909618"), 0.1909618)
    check("'-.7964761' 是负数不是缺失值", _to_float("-.7964761"), -0.7964761)
    check("'.' 才是缺失值", _to_float("."), None)
    check("'.a' 才是缺失值", _to_float(".a"), None)
    check("'.z' 才是缺失值", _to_float(".z"), None)

    # --- 5.2 xtreg 长写表头（Coefficient / std. err.）---
    coefs = auditor._extract_coefficients(LOG_REAL_XTREG_FE)
    check("xtreg 能抽出 3 行系数", len(coefs), 3)
    check("xtreg 系数名正确", [c["name"] for c in coefs], ["focus", "lev", "_cons"])
    focus = coefs[0] if coefs else {}
    check("xtreg 标准误不为 None（曾全部丢失）", focus.get("stderr"), 0.1909618)
    check("xtreg 置信区间下限正确", focus.get("ci_lo"), -1.170839)
    check("xtreg 置信区间上限正确", focus.get("ci_hi"), -0.4221136)
    check("xtreg 的 sigma_u / rho 不被当成系数",
          any(c["name"] in {"sigma_u", "rho", "sigma_e"} for c in coefs), False)

    # --- 5.3 聚类数写法（三种，实测都出现过）---
    from core.result_auditor import _find_cluster_count
    count, ev = _find_cluster_count(LOG_REAL_REG_HDFE)
    check("reghdfe: 'Number of clusters (id) = 5,373'", (count, ev), (5373, "Number of clusters (id)      =      5,373"))
    count, ev = _find_cluster_count(LOG_REAL_XTREG_FE)
    check("xtreg: 'Number of groups = 5,376'", count, 5376)

    # 第 (3) 种写法藏在脚注里，最容易漏 —— 实测用 regress, vce(cluster sector) 才发现
    log_vce_cluster = (
        ". regress tfp focus lev, vce(cluster sector)\n\n"
        "                                  (Std. err. adjusted for 12 clusters in sector)\n"
    )
    count, ev = _find_cluster_count(log_vce_cluster)
    check("regress: '(Std. err. adjusted for 12 clusters in sector)'", count, 12)
    report = auditor.audit("ols_basic", log_vce_cluster)
    check("12 个聚类 → 触发 few_clusters", "few_clusters" in codes(report), True)
    check("无回归表头时也不误判其他规则",
          [c for c in codes(report) if c != "few_clusters"], [])

    # --- 5.4 tabstat / misstable 不是系数表 ---
    coefs = auditor._extract_coefficients(LOG_REAL_TABSTAT)
    check("tabstat 输出不被当成系数表", coefs, [])
    coefs = auditor._extract_coefficients(LOG_REAL_MISSTABLE)
    check("misstable 输出不被当成系数表", coefs, [])

    # --- 5.5 真实日志整体判定 ---
    report = auditor.audit("fe", LOG_REAL_XTREG_FE)
    check("健康的真实 xtreg → 零误报", codes(report), [])
    report = auditor.audit("hdfe", LOG_REAL_REG_HDFE)
    check("健康的真实 reghdfe → 零误报", codes(report), [])
    report = auditor.audit("descriptive_stats", LOG_REAL_TABSTAT)
    check("真实的 tabstat → 零误报", codes(report), [])

    print()
    print(f"=== 结果：{passed} 通过 / {failed} 失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
