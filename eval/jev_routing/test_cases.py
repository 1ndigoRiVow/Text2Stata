# -*- coding: utf-8 -*-
"""
Jev 路由能力旁路评测 —— 测试集骨架

用途：评估 Jev（TypeSafe AI 的 System One 模型）能否承担 Text2Stata 的
      **模板路由** 这一步（从 13 个模板里选 1 个）。
      注意：本测试集只评路由，不评变量映射 —— Jev 不生成文本，做不了映射。

设计原则（三条，务必保持）：
  1. 只出中文，贴近真实用户措辞（这是产品的真实输入分布）。
  2. 覆盖三个类别的题目，比例大致 7 : 2 : 1
       - routing   正常路由：表述清楚，期望能明确选中某个模板或某一组
       - ambiguous 歧义：需求本身信息不足，期望系统"问回来"而不是硬猜
       - non_analysis 非分析请求：导数据、画图报表、闲聊等，不属于任何模板
  3. `expected` 一律用列表表达"可接受的答案集合"，避免把单点答案当唯一真理。
     `expected` 里出现 "ASK_USER" 或 "NO_TEMPLATE" 表示期望触发拒答而非路由。

字段说明（沿用 AML 评测那套习惯，便于出报告）：
  id            唯一编号，前缀 R/A/N 区分类别
  question      用户原话（评测的实际输入）
  scenario      这句话出现的业务场景，解释为什么这么问
  design_scope  这道题在考什么能力（路由精度 / 歧义识别 / 域外拒答）
  expected      可接受的模板名集合；含 ASK_USER / NO_TEMPLATE 表示期望不路由
  expected_impact  期望观察到的结果影响
  expected_result  判定标准（人工或脚本据此打对错）
  tags          便于分组统计的标签
"""

# ----------------------------------------------------------------------
# A0. 模板语义摘要 —— 这是被评测对象的"标签空间"
#     每个模板给出：官方名称 + 一句话使用场景 + 判据提示
#     这些文本会作为 Jev choice 的 criteria（也是现方案 prompt 的一部分）
# ----------------------------------------------------------------------
TEMPLATE_CATALOG = {
    "ols_basic": {
        "name": "基础 OLS 回归",
        "when": "最基础的稳健标准误 OLS 回归，适合快速基准分析",
    },
    "ols_full": {
        "name": "完整 OLS 诊断回归",
        "when": "要基准模型 + 控制变量模型 + 稳健标准误模型，并要 VIF、异方差检验、残差图等诊断",
    },
    "logit_binary": {
        "name": "二元因变量 Logit 回归",
        "when": "因变量是 0/1 二元变量，要边际效应和预测概率",
    },
    "descriptive_stats": {
        "name": "描述性统计",
        "when": "要样本量、均值、标准差、分位数、最小最大值",
    },
    "correlation_matrix": {
        "name": "相关系数矩阵",
        "when": "要变量之间的相关系数和显著性水平",
    },
    "data_quality_report": {
        "name": "数据质量检查",
        "when": "要查缺失值、重复值、异常分布等数据本身的问题",
    },
    "robust_cluster": {
        "name": "聚类稳健标准误回归",
        "when": "要回归但只要求聚类稳健标准误；没有个体变量时回退普通稳健标准误",
    },
    "fe": {
        "name": "固定效应模型",
        "when": "面板数据，要个体固定效应（xtreg, fe），已指定 id 和 time",
    },
    "hdfe": {
        "name": "双向固定效应模型，聚类到个体的稳健误",
        "when": "用 reghdfe 同时吸收个体和时间双向固定效应，并聚类到个体",
    },
    "panel_compare": {
        "name": "面板模型对比",
        "when": "要同时估计 Pooled OLS、固定效应、随机效应、双向固定效应做横向比较",
    },
    "first_difference": {
        "name": "一阶差分模型",
        "when": "要对变量做一阶差分后回归，处理不随时间变化的个体异质性",
    },
    "did_basic": {
        "name": "基础双重差分 DID",
        "when": "已构造好 treat_post 等 DID 交互项，要吸收个体时间固定效应并聚类",
    },
    "hausman": {
        "name": "豪斯曼检验，检验是否使用固定效应还是随机效应",
        "when": "要在固定效应和随机效应之间做选择，即做 Hausman 检验",
    },
}

# 拒答类伪标签
ASK_USER = "ASK_USER"          # 信息不足，应回问用户而不是猜
NO_TEMPLATE = "NO_TEMPLATE"    # 不属于任何现有模板，应明确告知做不了


# ----------------------------------------------------------------------
# A1. 正常路由题（should route）—— 期望命中模板
# ----------------------------------------------------------------------
ROUTING_CASES = [
    {
        "id": "R001",
        "question": "帮我跑一个最基础的最小二乘回归，因变量是 tfp，解释变量是 focus，用稳健标准误就行",
        "scenario": "入门用户要一个干净基准结果",
        "design_scope": "明确要 OLS + 稳健标准误，不应升级为固定效应",
        "expected": ["ols_basic"],
        "expected_impact": "应选中 ols_basic，不引入面板设定",
        "expected_result": "命中 ols_basic 即算对",
        "tags": ["regress", "explicit_model", "core"],
    },
    {
        "id": "R002",
        "question": "我想看一下 tfp 和 focus 的均值、标准差、中位数和四分位数",
        "scenario": "写作前的描述性统计表",
        "design_scope": "分位数/均值/标准差这类关键词指向描述统计",
        "expected": ["descriptive_stats"],
        "expected_impact": "应选中 descriptive_stats",
        "expected_result": "命中 descriptive_stats 即算对",
        "tags": ["describe", "explicit_model", "core"],
    },
    {
        "id": "R003",
        "question": "先帮我看看这几个变量两两之间的相关系数和显著性",
        "scenario": "多重共线性初筛",
        "design_scope": "相关系数 + 显著性 → 相关矩阵",
        "expected": ["correlation_matrix"],
        "expected_impact": "应选中 correlation_matrix",
        "expected_result": "命中 correlation_matrix 即算对",
        "tags": ["correlation", "explicit_model", "core"],
    },
    {
        "id": "R004",
        "question": "这批数据里 tfp 好像有挺多缺失的，帮我查一下缺失值、重复值和异常分布",
        "scenario": "回归前的数据体检",
        "design_scope": "缺失/重复/异常 → 数据质量报告",
        "expected": ["data_quality_report"],
        "expected_impact": "应选中 data_quality_report",
        "expected_result": "命中 data_quality_report 即算对",
        "tags": ["data_quality", "explicit_model", "core"],
    },
    {
        "id": "R005",
        "question": "我的因变量是虚拟变量，企业是否出口，跑一个 logit，看边际效应",
        "scenario": "二元因变量的概率模型",
        "design_scope": "0/1 因变量 + 边际效应 → logit_binary",
        "expected": ["logit_binary"],
        "expected_impact": "应选中 logit_binary",
        "expected_result": "命中 logit_binary 即算对",
        "tags": ["logit", "explicit_model", "core"],
    },
    {
        "id": "R006",
        "question": "用面板固定效应回归 tfp 对 focus，控制变量加 lev、size、growth，个体固定效应",
        "scenario": "最典型的面板设定，只吸收个体",
        "design_scope": "只提个体固定效应，未提时间 → 不应升级为 hdfe",
        "expected": ["fe"],
        "expected_impact": "应选中 fe 而非 hdfe —— 考察是否过度升级",
        "expected_result": "命中 fe 即算对；命中 hdfe 记为过度升级",
        "tags": ["panel", "explicit_model", "boundary", "core"],
    },
    {
        "id": "R007",
        "question": "做双向固定效应，同时吸收个体和年份，标准误聚类到个体层面",
        "scenario": "两个维度都要吸收",
        "design_scope": "明确双向 + 聚类 → hdfe",
        "expected": ["hdfe"],
        "expected_impact": "应选中 hdfe",
        "expected_result": "命中 hdfe 即算对；命中 fe 记为欠吸收",
        "tags": ["panel", "explicit_model", "boundary", "core"],
    },
    {
        "id": "R008",
        "question": "帮我把混合 OLS、固定效应、随机效应和双向固定效应都跑一遍，放在一张表里比较",
        "scenario": "模型选择章节需要横向对比",
        "design_scope": "同时要多个模型 → panel_compare",
        "expected": ["panel_compare"],
        "expected_impact": "应选中 panel_compare，而不是只跑 fe",
        "expected_result": "命中 panel_compare 即算对",
        "tags": ["panel", "explicit_model", "core"],
    },
    {
        "id": "R009",
        "question": "我不确定该用固定效应还是随机效应，帮我做个检验判断一下",
        "scenario": "模型设定检验",
        "design_scope": "FE vs RE 的取舍 → hausman 检验",
        "expected": ["hausman"],
        "expected_impact": "应选中 hausman",
        "expected_result": "命中 hausman 即算对；若只跑 fe 或只跑 re 记为漏掉检验意图",
        "tags": ["test", "explicit_model", "core"],
    },
    {
        "id": "R010",
        "question": "我想消除不随时间变化的个体异质性影响，用一阶差分的方式做，把变量都差一次再回归",
        "scenario": "稳健性检验里的一阶差分",
        "design_scope": "一阶差分表述 → first_difference（不是 fe）",
        "expected": ["first_difference"],
        "expected_impact": "应选中 first_difference；选 fe 属于混淆了两种消除个体效应的方式",
        "expected_result": "命中 first_difference 即算对",
        "tags": ["panel", "explicit_model", "boundary", "core"],
    },
    {
        "id": "R011",
        "question": "我已经构造好 treat_post 这个交互项了，帮我做双重差分，吸收个体和时间，聚类到个体",
        "scenario": "DID 政策评估",
        "design_scope": "已有交互项 + DID 关键词 → did_basic",
        "expected": ["did_basic"],
        "expected_impact": "应选中 did_basic，而不是普通的 hdfe",
        "expected_result": "命中 did_basic 即算对",
        "tags": ["did", "explicit_model", "boundary", "core"],
    },
    {
        "id": "R012",
        "question": "回归结果我想要稳健标准误，并且在企业层面聚类",
        "scenario": "标准误设定的单独诉求",
        "design_scope": "只讲标准误层级、未指定估计量 → robust_cluster",
        "expected": ["robust_cluster"],
        "expected_impact": "应选中 robust_cluster",
        "expected_result": "命中 robust_cluster 即算对",
        "tags": ["regress", "explicit_model", "core"],
    },
    {
        "id": "R013",
        "question": "我想要一套比较完整的结果：先基准回归，再加控制变量，然后稳健标准误，顺带做个 VIF 和异方差检验",
        "scenario": "论文主表的完整输出",
        "design_scope": "多模型递进 + 诊断 → ols_full",
        "expected": ["ols_full"],
        "expected_impact": "应选中 ols_full，而不是 ols_basic",
        "expected_result": "命中 ols_full 即算对；命中 ols_basic 记为降级",
        "tags": ["regress", "explicit_model", "boundary", "core"],
    },
    # ---- 口语化 / 同义表述，考察鲁棒性 ----
    {
        "id": "R014",
        "question": "跑个回归呗，看看 focus 对 tfp 有没有影响",
        "scenario": "口语化需求，未提标准误、未提面板",
        "design_scope": "信息偏少但可判：说得出 y 和 x，最小可跑模型",
        "expected": ["ols_basic", "robust_cluster"],
        "expected_impact": "可接受 OLS 系或聚类稳健系；不应跳到面板模型",
        "expected_result": "命中 ols_basic / robust_cluster 之一即算对",
        "tags": ["colloquial", "underspecified", "core"],
    },
    {
        "id": "R015",
        "question": "帮我控制一下企业层面那些不随时间变的东西，再跑 tfp 对 focus",
        "scenario": "用户不知道术语，用白话表达固定效应",
        "design_scope": "白话描述个体固定效应 → 应能映射到 fe",
        "expected": ["fe", "hdfe"],
        "expected_impact": "应识别出这是在说固定效应",
        "expected_result": "命中 fe / hdfe 之一即算对；命中 ols_basic 记为漏掉固定效应",
        "tags": ["colloquial", "semantic", "core"],
    },
    {
        "id": "R016",
        "question": "我想先看看数据长什么样，各变量的分布情况",
        "scenario": "探索性第一步",
        "design_scope": "分布 → 描述性统计或数据质量都可接受",
        "expected": ["descriptive_stats", "data_quality_report"],
        "expected_impact": "两者都合理，考察是否跑到回归去",
        "expected_result": "命中两者之一即算对；命中任何回归模板记为错",
        "tags": ["colloquial", "semantic", "core"],
    },
    {
        "id": "R017",
        "question": "用 reghdfe 做两向固定效应，absorb 企业代码和年份，cluster 到企业代码",
        "scenario": "懂 Stata 的用户直接点命令名",
        "design_scope": "点名 reghdfe → hdfe",
        "expected": ["hdfe"],
        "expected_impact": "应选中 hdfe",
        "expected_result": "命中 hdfe 即算对",
        "tags": ["explicit_command", "core"],
    },
    {
        "id": "R018",
        "question": "xtreg tfp focus lev size, fe 然后给我看看随机效应的对比",
        "scenario": "点了 Stata 命令，但后半句模糊",
        "design_scope": "说了 fe 又提 re 对比 → panel_compare 或 fe 均可",
        "expected": ["fe", "panel_compare"],
        "expected_impact": "至少应含固定效应；提了对比则 panel_compare 更贴",
        "expected_result": "命中 fe / panel_compare 之一即算对",
        "tags": ["explicit_command", "underspecified", "borderline"],
    },
]

# ----------------------------------------------------------------------
# A2. 歧义题（should ask back）—— 信息不足，期望不硬猜
# ----------------------------------------------------------------------
AMBIGUOUS_CASES = [
    {
        "id": "A001",
        "question": "帮我分析一下这批数据",
        "scenario": "完全没有指明分析目的",
        "design_scope": "零信息需求 → 必须回问",
        "expected": [ASK_USER],
        "expected_impact": "不应擅自选模板；应询问想做什么分析、关注哪些变量",
        "expected_result": "输出 ASK_USER/低置信度即算对；直接路由任一模板记为错（幻觉路由）",
        "tags": ["zero_info", "must_ask", "core"],
    },
    {
        "id": "A002",
        "question": "帮我做双向固定效应",
        "scenario": "说得出方法却没说变量",
        "design_scope": "方法明确但 y/x/id/time 全缺 → 回问变量（这题现方案已被拦住）",
        "expected": [ASK_USER, "hdfe"],
        "expected_impact": "可以定模板，但变量必须回问；若直接生成脚本即为失败",
        "expected_result": "输出 ASK_USER 算对；给出 hdfe 但标注需补变量也算对；静默产出脚本算错",
        "tags": ["method_only", "must_ask", "core"],
    },
    {
        "id": "A003",
        "question": "看看这个变量对那个变量有没有影响",
        "scenario": "指代不明，用“这个”“那个”",
        "design_scope": "指代歧义 → 必须回问是哪两个变量",
        "expected": [ASK_USER],
        "expected_impact": "不应猜变量名",
        "expected_result": "输出 ASK_USER 即算对；直接选模板并凑变量记为错",
        "tags": ["coreference", "must_ask", "core"],
    },
    {
        "id": "A004",
        "question": "跑个回归吧，我想看看结果好不好",
        "scenario": "既没说变量也没说模型，还带着主观判断",
        "design_scope": "零信息 + 主观标准 → 回问",
        "expected": [ASK_USER],
        "expected_impact": "“结果好不好”无法执行，应回问关注什么",
        "expected_result": "输出 ASK_USER 即算对",
        "tags": ["zero_info", "must_ask"],
    },
    {
        "id": "A005",
        "question": "帮我检验一下内生性",
        "scenario": "提出了方法学诉求，但当前模板库没有对应工具",
        "design_scope": "域内术语但库外能力 → 应说明限制，不硬塞模板",
        "expected": [NO_TEMPLATE, ASK_USER],
        "expected_impact": "不应伪装成 fe/ols 搪塞",
        "expected_result": "NO_TEMPLATE 或 ASK_USER 算对；路由到 fe/ols 记为错",
        "tags": ["out_of_library", "must_disclose", "core"],
    },
    {
        "id": "A006",
        "question": "我怀疑存在遗漏变量偏误，能不能帮我处理掉",
        "scenario": "同样超出模板库能力",
        "design_scope": "库外能力 → 明确告知做不到",
        "expected": [NO_TEMPLATE, ASK_USER],
        "expected_impact": "不能假装用固定效应就能解决",
        "expected_result": "NO_TEMPLATE 或 ASK_USER 算对",
        "tags": ["out_of_library", "must_disclose", "core"],
    },
    {
        "id": "A007",
        "question": "先做描述性统计，然后做固定效应，最后看看中介效应",
        "scenario": "多步请求，末步超出能力",
        "design_scope": "多意图 + 部分不可行 → 应拆解并说明哪步做不了",
        "expected": [ASK_USER, "descriptive_stats"],
        "expected_impact": "至少应识别出中介效应不在库内",
        "expected_result": "识别出不可行部分即算对；整体路由到单一模板记为错",
        "tags": ["multi_intent", "out_of_library", "borderline"],
    },
    {
        "id": "A008",
        "question": "用那个高级一点的方法跑一下",
        "scenario": "模糊的修饰性表述",
        "design_scope": "“高级”无实指 → 回问",
        "expected": [ASK_USER],
        "expected_impact": "不应自行解释为某个复杂模型",
        "expected_result": "输出 ASK_USER 即算对",
        "tags": ["vague", "must_ask"],
    },
    {
        "id": "A009",
        "question": "基准回归加双向固定效应，再加个中介效应检验，顺便看下调节效应",
        "scenario": "典型的需求堆叠，含库外项",
        "design_scope": "多意图混合 → 应指出能力边界",
        "expected": [ASK_USER, "hdfe"],
        "expected_impact": "不应把中介/调节硬塞给 hdfe",
        "expected_result": "明确指出中介/调节不可行即算对",
        "tags": ["multi_intent", "out_of_library", "core"],
    },
    {
        "id": "A010",
        "question": "不知道用哪个模型好，你觉得呢",
        "scenario": "把决策权完全交给系统",
        "design_scope": "需求为“给建议” → 应回问研究设计",
        "expected": [ASK_USER],
        "expected_impact": "不应随机挑一个模板",
        "expected_result": "输出 ASK_USER 即算对",
        "tags": ["delegating", "must_ask"],
    },
]

# ----------------------------------------------------------------------
# A3. 非分析请求（should reject）—— 不属于任何模板
# ----------------------------------------------------------------------
NON_ANALYSIS_CASES = [
    {
        "id": "N001",
        "question": "帮我把这个 dta 文件导出成 Excel",
        "scenario": "数据格式转换，不是统计分析",
        "design_scope": "域内工具请求但非分析 → 应明确不属模板范围",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应路由到任何统计模板",
        "expected_result": "输出 NO_TEMPLATE 即算对；返回任一模板记为错",
        "tags": ["out_of_scope", "core"],
    },
    {
        "id": "N002",
        "question": "帮我把这几个变量的名字改成中文",
        "scenario": "数据整理，不是分析",
        "design_scope": "变量重命名 → 域外",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应路由到统计模板",
        "expected_result": "输出 NO_TEMPLATE 即算对",
        "tags": ["out_of_scope", "core"],
    },
    {
        "id": "N003",
        "question": "帮我画一张 tfp 的时间趋势折线图",
        "scenario": "纯可视化请求",
        "design_scope": "画图不是现有模板能力（库内无绘图模板）",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应塞进描述性统计",
        "expected_result": "输出 NO_TEMPLATE 即算对",
        "tags": ["out_of_scope", "visualization", "borderline"],
    },
    {
        "id": "N004",
        "question": "把这段 do 文件里的报错修一下",
        "scenario": "拿系统当调试器",
        "design_scope": "代码调试请求 → 域外",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应路由到统计模板",
        "expected_result": "输出 NO_TEMPLATE 即算对",
        "tags": ["out_of_scope", "debug"],
    },
    {
        "id": "N005",
        "question": "今天上海的天气怎么样",
        "scenario": "完全域外，考察是否会被 schema 带偏",
        "design_scope": "域外流量 → 必须拒绝",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应因为数据里有 year 变量就去跑时间序列",
        "expected_result": "输出 NO_TEMPLATE 即算对；路由任一模板记为严重错误",
        "tags": ["out_of_domain", "core"],
    },
    {
        "id": "N006",
        "question": "帮我写一段论文的摘要，介绍我的研究结论",
        "scenario": "文本写作请求",
        "design_scope": "写作不是分析 → 域外",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应路由到统计模板",
        "expected_result": "输出 NO_TEMPLATE 即算对",
        "tags": ["out_of_domain", "writing"],
    },
    {
        "id": "N007",
        "question": "帮我把这份 30 页的 PDF 研报总结成 200 字",
        "scenario": "文档处理请求（与实际工作场景一致）",
        "design_scope": "文档摘要 → 域外",
        "expected": [NO_TEMPLATE],
        "expected_impact": "不应路由到统计模板",
        "expected_result": "输出 NO_TEMPLATE 即算对",
        "tags": ["out_of_domain", "summarization"],
    },
    {
        "id": "N008",
        "question": "帮我看看这段回归结果该怎么解释，系数是 -0.33，显著吗",
        "scenario": "结果解读请求",
        "design_scope": "解读不是执行 → 边界模糊，可接受两种处理",
        "expected": [NO_TEMPLATE, ASK_USER],
        "expected_impact": "不应重新跑一遍回归",
        "expected_result": "NO_TEMPLATE 或 ASK_USER 算对；返回回归模板记为错",
        "tags": ["out_of_scope", "interpretation", "borderline"],
    },
]


def all_cases():
    """返回合并后的全部题目，附带类别字段。"""
    rows = []
    for cat, cases in (
        ("routing", ROUTING_CASES),
        ("ambiguous", AMBIGUOUS_CASES),
        ("non_analysis", NON_ANALYSIS_CASES),
    ):
        for c in cases:
            item = dict(c)
            item["category"] = cat
            rows.append(item)
    return rows


if __name__ == "__main__":
    rows = all_cases()
    from collections import Counter

    print(f"测试集总题数: {len(rows)}")
    print("分类分布:", dict(Counter(r["category"] for r in rows)))
    tag_counter = Counter(t for r in rows for t in r["tags"])
    print("\n标签分布（前 12）:")
    for t, n in tag_counter.most_common(12):
        print(f"  {t:20s} {n}")
    print(f"\n模板覆盖检查（{len(TEMPLATE_CATALOG)} 个模板）:")
    covered = set()
    for r in rows:
        for e in r["expected"]:
            if e in TEMPLATE_CATALOG:
                covered.add(e)
    missing = sorted(set(TEMPLATE_CATALOG) - covered)
    print(f"  被至少一道题覆盖: {len(covered)}/{len(TEMPLATE_CATALOG)}")
    print(f"  未被覆盖: {missing or '（无）'}")
