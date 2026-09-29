"""
Text2Stata 结果校验层（Result Auditor）
=======================================

定位
----
`stata_worker` 只回答一个问题：**脚本跑通了吗？**
本模块回答第二个问题：**跑出来的结果像话吗？**

这两件事完全不同。2026 年的 CausalVerify 研究给出了量化证据：
259 篇经济学论文衍生的任务里，**15.5% 的 LLM 生成代码能无错跑通、却给出完全错误的处理效应**。
本项目的执行判定已经足够严格（默认判失败），但它拦不住这类错误 —— 脚本确实
`end of do-file` 了，退出码也确实是 0，只是结果没意义。

设计原则
--------
1. **只做客观检查，不做领域判断。**
   本层不做计量学推理（不判断识别策略对不对、不问该不该用 FE）。
   它只看"数字本身是否自相矛盾"：样本量为 0、系数无量、区间倒挂、严重共线。
   凡是需要判断力的检查都不属于这里 —— 那些应该固化进模板规格，而不是交给启发式规则。

2. **只提醒，不阻断。**
   校验结论通过 `status="warning"` + `warnings[]` 返回，结果照常可下载。
   理由是：正常分析也会触发一部分检查（例如有意为之的高共线性检验），
   硬阻断会误杀。把异常"变可见"而不是"变不可能"，这一步就已经消除了静默失败。

3. **误报比漏报更糟。**
   一条老是误报的警告，用户很快就不看了，等于没有。所以每条规则都必须在
   "看起来像异常"和"确实是异常"之间偏保守：宁可漏掉边缘情况，不发没把握的警报。

接口
----
    auditor = ResultAuditor()
    report = auditor.audit(template_name, log_text)
    # report = {
    #     "status": "passed" | "warning",
    #     "warning_count": int,
    #     "warnings": [ {code, level, title, detail, evidence}, ... ],
    # }

    # 可选：对已导出的 CSV 产物做磁盘级复核（低样本量、全缺失列等）
    extra = auditor.audit_artifacts(["descriptive_stats.csv"], workspace_dir)

新增一个模板的支持 = 加一组正则 + 一条规则注册，不需要改动调用方。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path


# ----------------------------------------------------------------------
# 通用工具：从 Stata 日志里取值
# ----------------------------------------------------------------------

# Stata 日志里数字的标准写法：1,234  1.234e+05  -0.5321  .
_NUMBER = r"[-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _to_float(token):
    """
    把 Stata 日志里的数字串转成 float；缺失值返回 None。

    ⚠️ 这里有个必须记住的陷阱：
    Stata 的缺失值是 `.` / `.a` / `.z`，但 Stata **同样大量使用省略前导零的小数**
    （`.1909618` 就是 0.1909618）。

    最初的实现写的是 `if token.startswith("."): return None`，
    结果把所有前导点小数都判成了缺失值 —— 表面上"能跑"，实际上
    整个标准误列全是 None，`invalid_stderr` 之类的检查全部失效。
    这个 bug 只在真实 Stata 日志上才暴露，手写的测试样本（都带前导零）完全看不出来。

    正确判据：点号后面还有数字 → 是小数；点号后面是空或字母 → 才是缺失值。
    """
    if token is None:
        return None
    token = str(token).strip().replace(",", "")
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        # 走到这里说明是 Stata 的缺失值标记（. / .a / .z / .b ...）
        return None


# 回归类输出里需要抽出来的三个核心量
_REGRESSION_PATTERNS = (
    re.compile(r"Number of obs\s*=\s*([\d,]+)", re.IGNORECASE),
    re.compile(r"R-squared\s*=\s*(" + _NUMBER + r")", re.IGNORECASE),
    re.compile(r"F\s*\(\s*\d+\s*,\s*\d+\s*\)\s*=\s*(" + _NUMBER + r")", re.IGNORECASE),
    re.compile(r"Prob\s*>\s*F\s*=\s*(" + _NUMBER + r")", re.IGNORECASE),
)

# 系数表里的数值列（Stata 固定宽度输出）。
#
# 关键：不同命令的表头文字和列间距都不一样，必须在真实日志上验证过才能写死。
#   regress   -> "price | Coef. Std. Err. t P>|t| [95% Conf. Interval]"   （缩写、点号）
#   xtreg     -> "tfp | Coefficient std. err. t P>|t| [95% conf. interval]"（长写、无点号）
# 所以：列之间用 \s+ 而不是固定宽度的空格数；
#       变量名列后可能带竖线（表格左边界）；标准误列名两种写法都要能对上。
_NUM_TOKEN = r"(?:" + _NUMBER + r"|\.)"
_COEF_ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+\|?\s*"
    r"(?P<coef>" + _NUM_TOKEN + r")\s+"
    r"(?P<stderr>" + _NUM_TOKEN + r")\s+"
    r"(?P<tstat>" + _NUM_TOKEN + r")\s+"
    r"(?P<pval>" + _NUM_TOKEN + r")\s+"
    r"(?P<ci_lo>" + _NUM_TOKEN + r")\s+"
    r"(?P<ci_hi>" + _NUM_TOKEN + r")\s*$",
    re.MULTILINE,
)

# 被抽成系数行的表头 / 脚注关键词，不能算作真实系数
_COEF_ROW_BLACKLIST = {
    "variable", "coef", "std", "err", "t", "p", "z", "beta", "robust",
    "obs", "f", "prob", "r", "adj", "root", "mse", "total", "model",
    "residual", "mean", "sd", "min", "max", "note", "source", "ss",
    "df", "ms", "number", "of", "coefficient", "interval", "conf",
    "sigma_u", "sigma_e", "rho", "corr", "within", "between", "overall",
}

# VIF 表：Variable | VIF | 1/VIF（同样容忍名字后的竖线）
_VIF_ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+\|?\s*(?P<vif>\d+\.\d+)\s+(?P<inv>\d+\.\d+)\s*$",
    re.MULTILINE,
)
_MEAN_VIF = re.compile(r"Mean\s+VIF\s*[=|\s]\s*(" + _NUMBER + r")", re.IGNORECASE)

# 聚类数 / 分组数。
#
# 实测出现过三种写法，缺任何一种都会让 few_clusters 规则静默失效：
#   (1) "Number of clusters (id)      =      5,373"   ← reghdfe
#   (2) "Number of groups  =      5,376"              ← xtreg, fe
#   (3) "(Std. err. adjusted for 12 clusters in sector)"  ← regress, vce(cluster ...)
#
# 第 (3) 种是最容易漏的：它藏在脚注里，字面没有 "Number of"，但恰恰是
# regress/xtreg 这类基础命令报告聚类数的唯一位置。
_CLUSTER_COUNT_PATTERNS = (
    re.compile(
        r"Number\s+of\s+(?:clusters|groups)\s*(?:\([^)]*\))?\s*=\s*([\d,]+)",
        re.IGNORECASE),
    re.compile(
        r"adjusted\s+for\s+([\d,]+)\s+clusters?\s+in\s+(\S+)",
        re.IGNORECASE),
)


def _find_cluster_count(log_text):
    """
    返回 (聚类数, 原始匹配文本)；找不到返回 (None, None)。

    多个模型同框时取**最小**的那个 —— 聚类数越少，标准误低估越严重，
    这是风险最高的情形，应当以它为准报警。
    """
    found = []
    for pattern in _CLUSTER_COUNT_PATTERNS:
        for match in pattern.finditer(log_text):
            token = match.group(1)
            try:
                found.append((int(token.replace(",", "")), match.group(0).strip()))
            except ValueError:
                continue
    if not found:
        return None, None
    return min(found, key=lambda item: item[0])

# Stata 的提示性警告（不是错误码，但值得转达）
_STATA_NOTE_PATTERNS = (
    (re.compile(r"\(?\d+\s+observations?\s+dropped\)?", re.IGNORECASE),
     "stata_dropped_obs", "样本被丢弃",
     "Stata 在估计过程中丢弃了部分观测（通常是缺失值或组内无变异）。"
     "请确认这是预期行为，并检查是否造成样本选择偏误。"),
    (re.compile(r"omitted\s+because\s+of\s+collinearity", re.IGNORECASE),
     "stata_collinearity", "变量因共线被自动剔除",
     "Stata 因完全共线自动剔除了变量。若被剔除的是核心解释变量，结论将不可用。"),
    (re.compile(r"note:\s+(\S+)\s+omitted", re.IGNORECASE),
     "stata_var_omitted", "变量被自动剔除",
     "某个变量在估计中被自动剔除。请查看日志确认剔除原因。"),
    (re.compile(r"has\s+missing\s+values|missing\s+values\s+encountered", re.IGNORECASE),
     "stata_missing", "存在缺失值",
     "数据中存在缺失值。请确认缺失比例，以及缺失是否与结果变量相关（会带来偏误）。"),
    (re.compile(r"convergence\s+not\s+achieved", re.IGNORECASE),
     "stata_convergence", "迭代未收敛",
     "模型迭代未收敛，估计结果不可信。通常需要调整模型设定或检查变量尺度。"),
    (re.compile(r"(\S+)\s+has\s+(\d+)\s+unique\s+values?\s+and\s+is\s+being\s+treated\s+as\s+missing",
                re.IGNORECASE),
     "stata_str_numeric", "变量被当作缺失处理",
     "某个变量被 Stata 当作字符串/缺失处理，未进入模型。请检查该变量的存储类型。"),
)

# 组内变异为零：FE / HDFE 的真实软肋 —— 脚本能跑通，估计无意义
_NO_WITHIN_VARIATION = re.compile(
    r"(\S+)\s+omitted\s+because\s+of\s+collinearity.{0,200}?"
    r"|within\s+variance\s+is\s+zero",
    re.IGNORECASE | re.DOTALL,
)


# ----------------------------------------------------------------------
# 规则定义
# ----------------------------------------------------------------------

class Warning_:  # noqa: N801 —— 与 Stata 的 warning 概念对齐，保留尾下划线避免遮蔽内置
    __slots__ = ("code", "level", "title", "detail", "evidence")

    def __init__(self, code, level, title, detail, evidence=None):
        self.code = code
        self.level = level          # "high" | "medium" | "low"
        self.title = title
        self.detail = detail
        self.evidence = evidence    # 触发这条规则时看到的具体数字/文本

    def to_dict(self):
        return {
            "code": self.code,
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
        }


_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


class ResultAuditor:
    """
    结果合理性校验器。无状态，可复用。

    阈值全部是模块级常量，便于之后按真实数据重新标定。
    """

    # ---- 阈值（可调）----
    MIN_OBS = 30                  # 低于此样本量，估计不可靠
    TINY_OBS = 10                 # 低于此样本量，几乎必然不可用
    MAX_ABS_COEF = 1e4            # 系数绝对值超过此量级，通常是量纲/共线导致
    HIGH_VIF = 10.0               # 单变量 VIF 警戒线
    HIGH_MEAN_VIF = 5.0           # 平均 VIF 警戒线
    MIN_CLUSTERS = 30             # 聚类数过少，聚类稳健标准误不可靠
    MAX_MISSING_SHARE = 0.5       # 单个变量缺失率超过此值

    # 这些模板的输出是回归表，才做系数层面的检查
    REGRESSION_TEMPLATES = {
        "ols_basic", "ols_full", "logit_binary", "robust_cluster",
        "fe", "hdfe", "panel_compare", "first_difference",
        "did_basic", "hausman",
    }

    def audit(self, template_name, log_text):
        """
        主入口：对一次执行结果做体检。

        :param template_name: 模板 key（决定做哪一组检查）
        :param log_text: Stata 完整日志
        :return: {"status", "warning_count", "warnings": [...]}
        """
        warnings: list[Warning_] = []
        log_text = log_text or ""

        if not log_text.strip():
            # 日志缺失属于执行层问题，stata_worker 已经判失败，这里不重复报
            return self._finalize(warnings)

        # --- 通用检查（所有模板）---
        warnings.extend(self._check_empty_results(log_text))
        warnings.extend(self._check_stata_notes(log_text))

        # --- 回归类检查 ---
        if template_name in self.REGRESSION_TEMPLATES:
            warnings.extend(self._check_regression(log_text))
            warnings.extend(self._check_collinearity(log_text))
            warnings.extend(self._check_clusters(log_text))
            if template_name in {"fe", "hdfe", "first_difference"}:
                warnings.extend(self._check_within_variation(log_text))

        # --- 描述性统计类 ---
        if template_name in {"descriptive_stats", "correlation_matrix", "data_quality_report"}:
            warnings.extend(self._check_descriptive(log_text))

        return self._finalize(warnings)

    # ------------------------------------------------------------------
    # 通用规则
    # ------------------------------------------------------------------
    def _check_empty_results(self, log_text):
        """样本量为 0：脚本跑完了，但什么都没估计出来。"""
        out = []
        for match in re.finditer(r"Number\s+of\s+obs\s*(?:\([^)]*\))?\s*=\s*([\d,]+)",
                                 log_text, re.IGNORECASE):
            obs = int(match.group(1).replace(",", ""))
            if obs == 0:
                out.append(Warning_(
                    "empty_sample", "high", "样本量为 0",
                    "回归使用了 0 个观测。这通常意味着核心变量全部是缺失值，"
                    "或变量名在数据中不存在。结果没有任何意义。",
                    "Number of obs = 0",
                ))
                break  # 报一次就够
        return out

    def _check_stata_notes(self, log_text):
        """Stata 自己在日志里留下的提示性警告 —— 这些是免费的信号，不该丢。"""
        out = []
        seen = set()
        for pattern, code, title, detail in _STATA_NOTE_PATTERNS:
            if code in seen:
                continue
            match = pattern.search(log_text)
            if not match:
                continue
            seen.add(code)
            # 数据质量报告模板本身就是来查缺失值的，不要对它报"存在缺失值"
            out.append(Warning_(
                code, "medium", title, detail,
                match.group(0).strip()[:160],
            ))
        return out

    # ------------------------------------------------------------------
    # 回归类规则
    # ------------------------------------------------------------------
    def _check_regression(self, log_text):
        out = []
        obs = self._extract_obs(log_text)

        if obs is not None and 0 < obs < self.TINY_OBS:
            out.append(Warning_(
                "tiny_sample", "high", "样本量过小",
                f"本次估计只用了 {obs} 个观测。在这个量级上，系数估计和显著性检验"
                "都不可靠，任何结论都不应采信。",
                f"Number of obs = {obs}",
            ))
        elif obs is not None and self.TINY_OBS <= obs < self.MIN_OBS:
            out.append(Warning_(
                "small_sample", "medium", "样本量偏小",
                f"本次估计使用了 {obs} 个观测，低于常用的 {self.MIN_OBS} 观测门槛。"
                "大样本渐近性质未必成立，标准误和 p 值需要谨慎解读。",
                f"Number of obs = {obs}",
            ))

        # 系数层面
        coefs = self._extract_coefficients(log_text)
        if coefs:
            huge = [c for c in coefs if c["coef"] is not None and abs(c["coef"]) > self.MAX_ABS_COEF]
            if huge:
                names = "、".join(c["name"] for c in huge[:3])
                out.append(Warning_(
                    "huge_coefficient", "high", "系数异常巨大",
                    f"变量 {names} 的系数绝对值超过 {self.MAX_ABS_COEF:,.0f}。"
                    "通常是变量量纲差异过大（例如一个变量以元为单位、另一个以万元为单位）"
                    "或严重共线造成。建议检查变量单位，或对变量取对数。",
                    "；".join(f"{c['name']}={c['coef']:.4g}" for c in huge[:3]),
                ))

            inverted = [c for c in coefs
                        if c["ci_lo"] is not None and c["ci_hi"] is not None and c["ci_lo"] > c["ci_hi"]]
            if inverted:
                out.append(Warning_(
                    "ci_inverted", "high", "置信区间上下限倒挂",
                    "置信区间的下限大于上限，说明输出解析异常或估计过程不稳定。",
                    "、".join(c["name"] for c in inverted[:3]),
                ))

            # 系数有效但标准误为 0 或负 —— 数值层面的不可能值
            bad_se = [c for c in coefs
                      if c["stderr"] is not None and c["stderr"] <= 0 and c["coef"] is not None]
            if bad_se:
                out.append(Warning_(
                    "invalid_stderr", "medium", "标准误异常",
                    f"变量 {'、'.join(c['name'] for c in bad_se[:3])} 的标准误为 0 或负值，"
                    "这在数值上不成立，可能来自共线或数值溢出。",
                    "；".join(f"{c['name']}: se={c['stderr']}" for c in bad_se[:3]),
                ))

            all_missing = [c for c in coefs if c["coef"] is None and c["stderr"] is None]
            if all_missing and len(all_missing) == len(coefs):
                out.append(Warning_(
                    "all_coef_missing", "high", "所有系数均为缺失",
                    "系数表中没有任何有效估计值。模型可能完全无法识别。",
                    f"共 {len(coefs)} 行系数，全部为缺失",
                ))

        # 拟合优度为负：只可能是设定出了大问题（或加了约束）
        r2 = self._extract_stat(log_text, r"R-squared\s*=\s*(" + _NUMBER + r")")
        if r2 is not None and r2 < -0.001:
            out.append(Warning_(
                "negative_r2", "medium", "R² 为负",
                f"报告的 R² = {r2:.4f} 为负值。除个别带约束的设定外，这通常意味着"
                "模型设定有误（例如工具变量过多、或面板设定与数据结构不匹配）。",
                f"R-squared = {r2}",
            ))

        return out

    def _check_collinearity(self, log_text):
        """VIF 检查 —— 只在日志里真的做了 VIF 时触发（ols_full 模板）。"""
        out = []
        vifs = []
        for match in _VIF_ROW.finditer(log_text):
            name = match.group("name")
            if name.lower() in _COEF_ROW_BLACKLIST:
                continue
            value = _to_float(match.group("vif"))
            if value is not None:
                vifs.append((name, value))

        if not vifs:
            return out

        high = [(n, v) for n, v in vifs if v > self.HIGH_VIF]
        if high:
            names = "、".join(n for n, _ in high[:4])
            out.append(Warning_(
                "high_vif", "medium", "存在严重多重共线",
                f"变量 {names} 的 VIF 超过 {self.HIGH_VIF:.0f}，解释变量之间高度相关。"
                "这会让系数估计不稳定（换一个样本，符号都可能变），"
                "建议检查变量间的经济含义重叠，或改用主成分/剔除冗余变量。",
                "；".join(f"{n}: VIF={v:.2f}" for n, v in high[:4]),
            ))

        mean_vif = None
        match = _MEAN_VIF.search(log_text)
        if match:
            mean_vif = _to_float(match.group(1))
        if mean_vif is not None and mean_vif > self.HIGH_MEAN_VIF:
            out.append(Warning_(
                "high_mean_vif", "low", "平均 VIF 偏高",
                f"平均 VIF = {mean_vif:.2f}，超过常用的 {self.HIGH_MEAN_VIF:.0f} 警戒线，"
                "整体共线程度偏高。",
                f"Mean VIF = {mean_vif}",
            ))
        return out

    def _check_clusters(self, log_text):
        """聚类数过少是实证研究里最常见的隐性错误之一，而且完全不影响脚本跑通。"""
        clusters, evidence = _find_cluster_count(log_text)
        if clusters is None or clusters >= self.MIN_CLUSTERS:
            return []
        level = "high" if clusters < 10 else "medium"
        return [Warning_(
            "few_clusters", level, "聚类数过少",
            f"聚类数只有 {clusters} 个。聚类稳健标准误会随聚类数减少而严重低估，"
            "导致 t 值虚高、显著性被夸大（这是实证研究中常见的假阳性来源）。"
            f"聚类数低于 {self.MIN_CLUSTERS} 时，建议改用 wild bootstrap 等方法，"
            "或直接把结论表述为相关关系而非因果关系。",
            evidence,
        )]

    def _check_within_variation(self, log_text):
        """固定效应模型的真实软肋：核心变量组内没有变异 → 系数被吸收掉。"""
        if not _NO_WITHIN_VARIATION.search(log_text):
            return []
        # 只有当被剔除的变量确实出现在共线提示里才算
        match = re.search(r"note:\s+(\S+)\s+omitted\s+because\s+of\s+collinearity",
                          log_text, re.IGNORECASE)
        omitted = match.group(1) if match else "某个变量"
        return [Warning_(
            "no_within_variation", "high", "变量被固定效应完全吸收",
            f"变量 {omitted} 在组内没有变异（不随时间变化），被固定效应吃掉了，"
            "因此没有系数可以估计。若它是你的核心解释变量，这个模型无法回答你的问题 —— "
            "需要换用一阶差分之外的策略（例如交互项、或改用个体层面的横截面分析）。",
            f"note: {omitted} omitted because of collinearity",
        )]

    # ------------------------------------------------------------------
    # 描述性统计类规则
    # ------------------------------------------------------------------
    def _check_descriptive(self, log_text):
        """
        描述性统计链路的检查。
        descriptive.do 用 tabstat 输出 n/mean/sd/min/p25/median/p75/max，
        只要有一列的 N 为 0/缺失，就说明该变量全是缺失值 —— 后续任何回归都不可能用它。
        """
        out = []
        # tabstat 输出形如：   tfp |  1200  .5234  .1123  ...
        row = re.compile(
            r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(?P<n>[\d,\.]+)",
            re.MULTILINE,
        )
        empty_vars = []
        for match in row.finditer(log_text):
            name = match.group("name")
            if name.lower() in _COEF_ROW_BLACKLIST or name.lower() == "variable":
                continue
            n = _to_float(match.group("n"))
            if n is not None and n == 0:
                empty_vars.append(name)
        if empty_vars:
            out.append(Warning_(
                "all_missing_variable", "high", "存在全缺失变量",
                f"变量 {'、'.join(empty_vars[:4])} 的有效观测数为 0，即全部是缺失值。"
                "请确认变量名是否拼写正确 —— 如果变量名错了，Stata 有时不会报错而是跳过。",
                "；".join(empty_vars[:4]),
            ))
        return out

    # ------------------------------------------------------------------
    # 磁盘级复核（可选，针对已导出的 CSV 产物）
    # ------------------------------------------------------------------
    def audit_artifacts(self, files, workspace_dir):
        """
        对导出的 CSV 产物再做一层磁盘复核。

        为什么需要：日志里的数字是转述，CSV 是原始事实。
        某些问题（例如整列为空）在日志里看不出来，在 CSV 里一目了然。

        :param files: 产物文件名列表（相对 workspace）
        :param workspace_dir: workspace 绝对路径
        :return: 追加的 warnings 列表（dict）
        """
        out = []
        workspace = Path(workspace_dir)
        for name in files or []:
            if not str(name).lower().endswith(".csv"):
                continue
            path = workspace / Path(name).name
            if not path.exists():
                continue
            try:
                probe = self._probe_csv(path)
            except OSError:
                continue
            if probe is None:
                continue
            rows, empty_cols = probe

            if rows == 0:
                out.append(Warning_(
                    "csv_no_rows", "high", "结果文件没有数据行",
                    f"导出的 {name} 只有表头、没有数据行，说明上一步的运行结果为空。",
                    f"{name}: 0 data rows",
                ).to_dict())

            if empty_cols:
                out.append(Warning_(
                    "csv_empty_column", "medium", "结果文件存在空列",
                    f"{name} 中以下列所有单元格为空：{'、'.join(empty_cols[:5])}。"
                    "这通常意味着对应的统计量未被成功计算或导出。",
                    f"{name}: {', '.join(empty_cols[:5])}",
                ).to_dict())
        return out

    @staticmethod
    def _probe_csv(path, sample_limit=500):
        """读 CSV 的样本行，返回 (数据行数, 全空列名列表)；解析失败返回 None。"""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
                if not header:
                    return None
                non_empty = [0] * len(header)
                rows = 0
                for row in reader:
                    if not row or all(not cell.strip() for cell in row):
                        continue
                    rows += 1
                    for index, cell in enumerate(row[:len(header)]):
                        if cell.strip():
                            non_empty[index] += 1
                    if rows >= sample_limit:
                        break
        except (csv.Error, StopIteration):
            return None
        if rows == 0:
            return 0, []
        empty_cols = [header[i] for i, count in enumerate(non_empty) if count == 0]
        return rows, empty_cols

    # ------------------------------------------------------------------
    # 日志解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_obs(log_text):
        """取日志中最后一次出现的 Number of obs（多模型输出时保留最后一个）。"""
        values = re.findall(r"Number\s+of\s+obs\s*(?:\([^)]*\))?\s*=\s*([\d,]+)",
                            log_text, re.IGNORECASE)
        if not values:
            return None
        try:
            return int(values[-1].replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _extract_stat(log_text, pattern):
        """取最后一个匹配的统计量数值。"""
        matches = re.findall(pattern, log_text, re.IGNORECASE)
        for token in reversed(matches):
            value = _to_float(token)
            if value is not None:
                return value
        return None

    @staticmethod
    def _extract_coefficients(log_text):
        """
        从 Stata 的系数表里抽行。

        实现要点（全部来自真实日志的教训）：
        1. **逐行处理，不用 MULTILINE 的 finditer。**
           finditer + ^...$ 在长日志上会跨行滑动匹配，把相邻行拼成一行，
           导致列错位（实测 `_cons` 的 stderr 被吃成 None）。
        2. **必须要求 7 列齐整。**
           Stata 的系数表每行固定是「变量名 [竖线] coef se t p ci_lo ci_hi」。
           少于 7 列的行一律丢弃 —— 宁可漏，不要错位。
        3. **黑名单过滤表头与脚注。**
           "Number of obs"、"sigma_u"、"R-squared" 这类行会被列数检查拦掉一部分，
           剩下的靠黑名单兜住。
        """
        out = []
        for raw_line in log_text.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            match = _COEF_ROW.match(line)
            if not match:
                continue
            name = match.group("name")
            if name.lower() in _COEF_ROW_BLACKLIST:
                continue
            out.append({
                "name": name,
                "coef": _to_float(match.group("coef")),
                "stderr": _to_float(match.group("stderr")),
                "tstat": _to_float(match.group("tstat")),
                "pval": _to_float(match.group("pval")),
                "ci_lo": _to_float(match.group("ci_lo")),
                "ci_hi": _to_float(match.group("ci_hi")),
            })
        return out

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    @staticmethod
    def _finalize(warnings):
        # 同一 code 只保留一条（例如日志里多个模型都提示 dropped obs）
        unique = {}
        for item in warnings:
            unique.setdefault(item.code, item)
        ordered = sorted(unique.values(), key=lambda w: _LEVEL_ORDER.get(w.level, 9))
        return {
            "status": "warning" if ordered else "passed",
            "warning_count": len(ordered),
            "warnings": [w.to_dict() for w in ordered],
        }
