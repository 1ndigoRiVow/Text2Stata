# Text2Stata

用自然语言描述分析需求，上传 Stata 数据，得到可复现的 `.do` 脚本与结果。

面向经管社科背景、需要做实证分析但不想被 Stata 命令行卡住的人。目标是把数据分析的重心从「写代码」挪回「提问题、看结果」。

---

## 核心主张：不让大模型直接写代码

这是本项目与「让 AI 生成 Stata 代码」的最大区别，也是全部设计的出发点。

大模型直接写代码的问题不是写不对，而是**错得很像对的**。已有的实证研究表明，LLM 生成的因果推断代码里有相当比例能够无错跑通、却给出完全错误的处理效应；而且模型自报的置信度无法区分对错。对一个要产出研究结论的工具来说，这是不可接受的失败模式。

所以这里的做法是：

> **大模型只负责理解意图，输出一份结构化的「分析计划」——选定哪个模板、每个变量对应到谁。真正的 Stata 代码由确定性的模板引擎装配。**

```json
{
  "selected_template": "fe",
  "variable_mapping": {
    "y": "tfp", "x": "focus",
    "controls": ["lev", "size"],
    "id": "id", "time": "year"
  }
}
```

这样做换来三件事：

1. **错误可定位**。模型只在「选模板 + 映射变量」这两步可能出错，出错了也看得见；语法层面的错误被模板消灭了。
2. **结果可复现**。同一个计划永远装配出同一份脚本。模板经过验证，不会因为一次采样而漂移。
3. **能判断对错**。因为计划是结构化的，可以和数据集 Schema 做机械校验——变量不存在、必需变量缺失，在跑之前就能拦下来。

模型的自由度被收窄到它真正擅长的部分：理解一句中文在说什么。

---

## 架构

```
用户请求 + 数据集
      │
      ▼
┌─────────────────────────────────────────┐
│ 控制平面（模型参与）                      │
│  意图解析 → 结构化分析计划                 │
│  · 选模板  · 变量映射                     │
│  · 必需变量校验（缺失直接 400，不生成脚本）  │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│ 执行平面（确定性，模型不参与）              │
│  模板装配 → 组装 .do → 调 Stata 执行       │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│ 判定层（两件不同的事）                     │
│  stata_worker   跑通了吗？ 默认判失败，       │
│                 需正向证据才判成功          │
│  result_auditor 结果像话吗？ 只提醒，不阻断    │
└─────────────────────────────────────────┘
      │
      ▼
   日志 / 回归表 / 图表 / 结果体检报告
```

### 为什么执行判定要「默认判失败」

Stata 的执行失败有很多种不说话的方式：授权过期、可执行文件路径错、进程崩溃、脚本语法畸形。早先的判定逻辑是「日志里搜不到 `r(***)` 就算成功」——结果在学校授权到期后，系统持续回报「执行成功」，而实际上一个字都没跑。

现在的判定要求**四项正向证据同时成立**：退出码为 0、日志存在且非空、含 `end of do-file`、无 `r(NNN)`。

注意 `end of do-file` **不能单独作为成功依据**——脚本报错时它照样会打印。真实日志里报错脚本的尾部是 `end of do-file` 紧接着 `r(109)`。

### 为什么校验层不做计量学推理

`result_auditor` 只查**客观自相矛盾**：样本量为 0、系数是缺失值、置信区间上下颠倒、标准误无效、聚类数过少、变量被固定效应吸收……这些不需要领域判断，对就是对，错就是错。

它**不判断**该不该用固定效应、识别策略是否成立——那些需要计量经济学判断，写成启发式规则必然误报，而一条总在误报的警告等于没有警告。

校验结果只做提示，不阻断结果交付。

---

## 分析模板（13 个）

| 模板 | 说明 | 必需变量 |
|---|---|---|
| `ols_basic` | 基础 OLS 回归 | y, x |
| `ols_full` | 完整 OLS 诊断回归 | y, x |
| `logit_binary` | 二元因变量 Logit | y, x |
| `descriptive_stats` | 描述性统计 | y, x |
| `correlation_matrix` | 相关系数矩阵 | y, x |
| `data_quality_report` | 数据质量检查 | y, x |
| `robust_cluster` | 聚类稳健标准误 | y, x |
| `fe` | 固定效应模型 | y, x, id, time |
| `hdfe` | 双向固定效应 + 聚类稳健误 | y, x, id, time |
| `panel_compare` | 面板模型对比 | y, x, id, time |
| `first_difference` | 一阶差分模型 | y, x, id, time |
| `did_basic` | 基础双重差分 | y, x, id, time |
| `hausman` | 豪斯曼检验 | y, x, id, time |

模板若依赖用户自装命令（`esttab` / `outreg2` / `reghdfe` / `winsor2`），脚本自带 `capture which` + `ssc install` 守卫，缺什么装什么（`reghdfe` 依赖 `ftools`，安装顺序有讲究）。

注册表是唯一入口：`backend/core/templates/registry.json`。

---

## 快速开始

### 环境要求

- Python 3.10+
- 本机安装 Stata（15 及以上；SE 版即可），并能以命令行方式调用
- 一个 OpenAI 兼容的 `/v1/chat/completions` 接口

### 安装

```bash
pip install -r requirements.txt
```

### 配置

在项目根目录建 `.env`（已被 gitignore，不要提交）：

```ini
# 模型接口
TEXT2STATA_API_URL=https://your-endpoint/v1/chat/completions
TEXT2STATA_API_KEY_LINE1=sk-...
TEXT2STATA_DEFAULT_MODEL=gpt-4o

# Stata 可执行文件
TEXT2STATA_STATA_PATH=C:\Program Files\StataNow19\StataSE-64.exe

# 可选
TEXT2STATA_MAX_UPLOAD_MB=100
TEXT2STATA_CORS_ORIGINS=http://127.0.0.1:5000,http://localhost:5000,null
TEXT2STATA_FLASK_DEBUG=false
```

### 启动

```bash
cd backend && python app.py
```

服务起在 `http://127.0.0.1:5000`。前端是单页 HTML，直接用浏览器打开 `frontend/index.html` 即可（CORS 已放行 `null` 来源，支持 `file://` 直开）。

### 试一下

仓库里的 `testdata.dta` 是一份可以直接跑的演示数据：93 家上市公司 × 14 年（2011–2024）的非平衡面板，38 个变量，含资产负债率、全要素生产率、研发投入、省级政策注意力配置等，带完整中文变量标签。

可以试着输入：「用固定效应模型跑一下政策注意力对企业全要素生产率的影响，控制资产负债率和企业规模」。

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传 `.dta`，返回 `file_id` 与数据 Schema |
| POST | `/api/plan` | 自然语言需求 → 结构化分析计划（不执行） |
| POST | `/api/analyze` | 提交执行任务，返回 `task_id` |
| GET | `/api/task/<task_id>` | 轮询任务状态与结果 |
| GET | `/api/download/<filename>` | 下载分析产物 |
| GET | `/api/health` | 健康检查 |

任务状态：`pending` → `running` → `success` / `failed` / `error`。

---

## 测试

回归测试不依赖真实 Stata（除少数路径自检），可以直接跑：

```bash
cd backend

# 执行判定：锁死「假装成功」的各种场景（13 项）
python tests/test_execution_guard.py

# 结果校验：解析真实 Stata 日志格式（50 项）
python tests/test_result_audit.py
```

`test_result_audit.py` 里锁了几个**只在真实日志上才会暴露**的坑，它们都曾造成静默失效：

- **Stata 用省略前导零的小数**（`.1909618` 表示 0.1909618），而缺失值也是点号。按「以点开头就是缺失」判断，会让整列标准误变成 `None`，静默废掉所有标准误相关的检查。正确做法是靠 `float()` 转换失败来判缺失。
- **表头文字随命令变**：`regress` 打出 `Coef. / Std. Err.`，`xtreg` 打出 `Coefficient / std. err.`。按固定列宽写死正则会导致整张系数表抽不出来。
- **聚类数有三种写法**：`Number of clusters (id) = 5,373`（reghdfe）、`Number of groups = 5,376`（xtreg）、以及藏在脚注里的 `(Std. err. adjusted for 12 clusters in sector)`（regress）。第三种最容易漏。

改动 `stata_worker` 或 `result_auditor` 后请务必跑这两套测试。

---

## 目录结构

```
backend/
  app.py                    Flask 入口，6 个接口
  core/
    config.py               配置与 .env 加载
    llm_agent.py            意图解析
    logic_center.py         数据结构判定与头部生成
    template_manager.py     模板注册表与脚本装配
    stata_worker.py         Stata 子进程执行与日志解析
    result_auditor.py       结果校验层
    templates/              模板库（registry.json 为唯一注册表）
  tests/                    回归测试
frontend/
  index.html                单页前端
eval/jev_routing/           旁路评测：意图路由能力评估（不进主链路）
流程图/系统架构.md            架构图
需求文档.docx                项目需求与背景
testdata.dta                演示数据（抽样样本）
```

---

## 已知限制与后续方向

**尚未实现**

- **错误自愈重试**：执行失败时会给出 `diagnosis` 分类，但没有自动把错误反馈给模型重出计划再跑一遍的闭环。需求文档里列为核心功能，目前只完成了一半。
- **可复现存档**：还没有把每次运行的 `.do`、数据指纹、模板版本与模型版本一起落盘。目前「可复现」靠的是模板确定性，缺少运行级别的存档。
- **模板规格与校验器**：模板的 `required_globals` 是硬编码的列表，还没有一套可声明的规格语言来描述前置条件与后置断言。

**已知不足**

- 结果校验层的阈值（最小样本量、VIF 上限、最小聚类数）是拍出来的常量，还没有用真实运行数据标定。
- 前端 API 地址硬编码为 `127.0.0.1:5000`。
- 模板覆盖集中在横截面与面板模型，工具变量、断点回归、合成控制等还未纳入。

**值得一提**

`eval/jev_routing/` 下有一份关于「用专用路由模型替代通用大模型做意图解析」的旁路评测。结论是不建议替换（准确率净退步，延迟也没优势），但发现它在**高置信度时零错误**，可以当「不确定性探针」用。详见该目录下的 `FINDINGS.md`。

---

## 关于这份代码

本项目由一名财经院校经济学本科生借助 coding agent 全栈开发。开发者在四年里因课程作业、大创与毕业论文频繁使用 Stata，深感它对没有编程基础的经管学生门槛偏高，而市面上同类产品多以 VS Code 插件形式存在——对本就用得吃力的学生来说，先学会装插件和配环境又是一道坎。

于是有了这个项目。它可能做得并不好，但如果能起到一点抛砖引玉的作用，吸引更多人认真解决这个问题，那它的目的就达到了。
