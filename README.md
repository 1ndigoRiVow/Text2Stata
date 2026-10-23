# Text2Stata

把自然语言变成 Stata 实证分析的工具：用中文描述需求、上传 `.dta` 数据，得到可复现的 `.do` 脚本、回归结果和一份自动体检报告。

架构上不让大模型写代码，LLM 只输出结构化的分析计划，真正的 Stata 代码由确定性模板引擎装配，交给本机 Stata 执行；执行层与校验层各自独立把关。项目计划有两个发行形态：**本地一键包**（双击即用）和**云端版**（只做调度与界面）。

面向经管社科背景，需要做实证分析但没有统计分析软件使用基础的人。

---

## 核心主张：不让大模型直接写代码

这是本项目与「让 AI 生成 Stata 代码」的最大区别，也是全部设计的出发点。

大模型直接写代码的问题不是写不对，而是代码不能稳定输出，且有问题的代码零基础的人也看不懂，甚至不知道变量怎么填进命令行。在本人做过的实证分析中，LLM 生成的因果推断代码里有相当比例能够无错跑通，但是会给出完全错误的处理效应；而且模型自报的置信度无法区分对错。对一个要产出研究结论的工具来说，这是不可接受的失败模式。

所以作为一个比较死板的、一行一个命令一次输出的统计分析软件来说，需要规范模型的代码输出。经斟酌，决定如下形式：

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

这样的边际效益有三层：

1. **错误可定位**。模型只在「选模板 + 映射变量」这两步可能出错，出错了也看得见；至于语法层面的错误不会存在，因为装配前会经人工（我）审核。
2. **结果可复现**。同一个计划永远装配出同一份脚本。模板经过验证，不会因为一次采样而漂移。
3. **能判断对错**。因为计划是结构化的，可以和数据集Schema做机械校验。如果有变量不存在或者必需变量缺失的情况，在跑之前就可以拦截。

模型的自由度被收窄到其真正擅长的部分，即理解自然语句。

---

## 架构

```
用户请求 + 数据集
      │
      ▼
 ───────────────────────────────────────── 
  控制平面（模型参与）                       
   意图解析 → 结构化分析计划                 
   · 选模板  · 变量映射                      
   · 必需变量校验（缺失直接 400，不生成脚本）  
 ───────────────────────────────────────── 
      │
      ▼
 ───────────────────────────────────────── 
  执行平面               
   模板装配 → 组装 .do → 调 Stata 执行        
 ───────────────────────────────────────── 
      │
      ▼
 ───────────────────────────────────────── 
  判定层                      
   stata_worker   跑通了吗？ 默认判失败，        
                  需正向证据才判成功           
   result_auditor 结果像话吗？ 只提醒，不阻断     
 ─────────────────────────────────────────
      │
      ▼
   日志 / 回归表 / 图表 / 结果体检报告
```

### 为什么执行判定要默认判失败

Stata 的执行失败有很多种乱七八糟的类型，比方说license过期、路径错、进程崩了、脚本语法畸形。我一开始设想的判定逻辑是「有 `r(***)` 就判定失败」，然后license到期了，系统持续回报「执行成功」，实际上跑了滚木。

现在的判定要求同时满足四项正向证据同时成立：退出码为 0、日志存在且非空、含 `end of do-file`、无 `r(NNN)`。

注意 `end of do-file` **不能单独作为成功依据**——脚本报错时它照样会打印。真实日志里报错脚本的尾部是 `end of do-file` 紧接着 `r(109)`。

### 为什么校验层不做计量学推理

`result_auditor` 只查**客观自相矛盾**：样本量为 0、系数是缺失值、置信区间上下颠倒、标准误无效、聚类数过少、变量被固定效应吸收……这些不需要领域判断，对就是对，错就是错。

它**不判断**该不该用固定效应、识别策略是否成立——那些需要计量经济学判断，写成启发式规则必然误报，而一条总在误报的警告等于没有警告。

校验结果只做提示，不阻断结果交付。

### 模型选型

大模型在本项目里只干一件事：把「中文需求 + 数据 schema + 模板注册表」翻译成
**结构化 JSON 计划**。输入约 3~6k token，输出约 0.3k token，本质是
**闭集分类 + 槽位填充**，不吃推理能力。

所以选型标准是 **JSON 稳定性 > 中文 > 速度 > 价格**，而不是综合 benchmark 排名。
当前推荐模型为 `gpt-6.1-sol`（`core/model_catalog.py` 的 `RECOMMENDED_MODEL`），
由 `eval/model_picker` 在本项目自己的 12 道路由题上实测得出。（当然也可以换模型，正在做）

---

## 分析模板（28 个）

### 数据清洗

| 模板 | 说明 | 必需变量 |
|---|---|---|
| `missing_handler` | 缺失值处理：并排给按行删除 / 均值插补 / 面板填充三种方案对比 | y, x |
| `outlier_trim` | 异常值识别与截尾：3σ 与 1.5×IQR 两种口径，重跑基准回归 | y, x |
| `transform_vars` | 变量变换：对数 / 平方根 / 标准化 / 平方，含偏度峰度对比 | y, x |
| `panel_balance` | 面板平衡化：tsfill 补齐 vs 强平衡，两种方案的代价对比 | id, time |
| `vif_screen` | 共线性诊断与变量筛选：逐步剔除 VIF > 10 的控制变量 | y, x |
| `deduplicate` | 重复观测处理：按个体-时间组合查重，先列后删 | id |

清洗类模板的产物是**处理报告**（csv/rtf），清洗后的数据存为 `cleaned_data.dta` 留在执行目录，**不随结果回传**。这是故意的：云端版的前提就是数据不出本机。

### 回归与描述

| 模板 | 说明 | 必需变量 |
|---|---|---|
| `ols_basic` | 基础 OLS 回归 | y, x |
| `ols_full` | 完整 OLS 诊断回归（VIF、异方差、拟合图） | y, x |
| `robust_cluster` | 聚类稳健标准误 | y, x |
| `quantile_reg` | 分位数回归（0.25 / 0.5 / 0.75） | y, x |
| `tobit_model` | Tobit 模型（左截断于 0） | y, x |
| `count_model` | 计数模型（Poisson vs 负二项） | y, x |
| `logit_binary` | 二元因变量 Logit | y, x |
| `descriptive_stats` | 描述性统计 | y, x |
| `correlation_matrix` | 相关系数矩阵 | y, x |
| `data_quality_report` | 数据质量检查（缺失、重复、分布） | y, x |
| `winsorize` | 缩尾预处理（1% / 99%） | y, x |

### 面板

| 模板 | 说明 | 必需变量 |
|---|---|---|
| `fe` | 固定效应模型 | y, x, id, time |
| `hdfe` | 双向固定效应 + 聚类稳健误 | y, x, id, time |
| `panel_compare` | 面板模型对比（OLS / FE / RE / 双向 FE） | y, x, id, time |
| `first_difference` | 一阶差分模型 | y, x, id, time |
| `hausman` | 豪斯曼检验 | y, x, id, time |
| `dynamic_panel` | 动态面板 GMM（Arellano-Bond） | y, x, id, time |

### 因果推断

| 模板 | 说明 | 必需变量 |
|---|---|---|
| `did_basic` | 基础双重差分 | y, x, id, time |
| `event_study` | 事件研究法（平行趋势检验 + 系数图） | y, id, time, policy |
| `iv_2sls` | 工具变量两阶段最小二乘 | y, x, iv |
| `psm` | 倾向得分匹配（ATT + 平衡性检验） | y, x |
| `mediation` | 中介效应（江艇两步法） | y, x, m |

变量角色：`y` 被解释变量、`x` 核心解释变量、`controls` 控制变量、`id` 个体、`time` 时间，
以及进阶角色 `iv` 工具变量、`m` 中介变量、`policy` 政策实施期（变量名或直接填年份）、`treat` 处理组标识。

`mediation` 走的是江艇（2022）《因果推断经验研究中的中介效应与调节效应》的两步法：
先估 X 对 M 的效应，再看 M 与 Y 的相关性方向。**不做** Sobel 检验，也**不报**中介效应占比——那要求 M→Y 那一步也被识别，而这个方法恰恰不主张把它当成因果识别。

模板若依赖用户自装命令（`esttab` / `outreg2` / `reghdfe` / `winsor2` / `ivreg2` / `xtabond2` / `psmatch2` / `coefplot`），脚本自带 `capture which` + `ssc install` 守卫，缺什么装什么（`reghdfe` 依赖 `ftools`，安装顺序有讲究）。

注册表是唯一入口：`backend/core/templates/registry.json`。

**新增一个模板** = 一个 `.do` 文件 + 注册表一条记录（声明 `required_globals` / `optional_globals` / `output_files`）。两条硬性约定：

- 用到自装命令必须带 `capture which` + `ssc install` 守卫。
- **主回归不要加 `quietly`**。加上之后回归表会从日志里整块消失，执行层照报成功，
  但结果校验层解析不到系数表，体检等于没装（2026-10-15 修过一轮，波及 9 个模板）。
  只为取统计量（`quietly sum` 拿 `r(mean)` 之类）的可以保留。
- **清洗类模板不要删核心解释变量**。共线性的处理对象是控制变量；`$x` 和别人共线
  属于模型设定问题，删掉它等于把要研究的东西删了（`vif_screen` 里 `$x` 受保护）。
- **清洗类模板要能在脏数据上跑起来**。它们本来就是用来修重复观测的，
  如果自身依赖 `xtset`（个体-时间组合有重复时会失败），就没有自救通道了。

### 写模板的两条硬规矩

1. **不要在主回归上加 `quietly`。** 回归输出必须留在主日志里——结果校验层解析的就是主日志，
   一旦被抑制，系数表拿不到，体检会静默失效（只有 `fe` / `hdfe` 这类没写 `quietly` 的能被体检到）。
   取统计量用的 `quietly summarize` 之类不受此限。
2. **不要用 `log using` 另开日志文件。** 输出会多写一份没人看的 txt，而产物收集只认
   `.rtf / .doc / .docx / .csv / .png / .gph`，txt 从来下载不到。要落文件就导出 csv 或 rtf。

---

## 快速开始

两种方式都是**本地版**（全部跑在自己机器上）。想要网页形态、发给熟人用的**云端版**，见文末「关于云端版」。

### 方式一：解压即用（Windows，推荐）

下载 `Text2Stata-windows.zip`，解压后双击 `Text2Stata.exe`。

控制台会打印访问地址，浏览器会自动打开。不需要装 Python，不需要配依赖。首次运行会直接跳到设置面板，填两样东西：

1. **Stata 路径** —— 通常不用手填，点「自动探测」会从注册表的文件关联里找出来
2. **API Key** —— 任何兼容 OpenAI `/v1/chat/completions` 的服务都可以（也可以邮件或者其他渠道联系我，审核后会给你一个key，有10r额度熔断）

数据与配置存在 `%APPDATA%\Text2Stata\`，程序放在哪、覆盖升级都不会丢。

> 只提供 Windows 包。Mac / Linux 用户走下面的源码方式。
> 打包不含 Stata ，需要自备授权，这个确实没啥办法。

自己打包：

```bash
pip install -r requirements-build.txt
python packaging/build.py
```

### 方式二：从源码运行

#### 环境要求

- Python 3.10+
- 本机安装 Stata（15 及以上；SE 版即可），并能以命令行方式调用
- 一个 OpenAI 兼容的 `/v1/chat/completions` 接口

#### 安装

```bash
pip install -r requirements.txt
```

#### 配置

两种方式，任选：

- **在网页里配**（推荐）：启动后点侧栏「设置」，改完立即生效，写入 `backend/config.json`（已 gitignore）
- **用 `.env`**：在项目根目录建 `.env`（已被 gitignore，不要提交）

```ini
# 模型接口
TEXT2STATA_API_URL=https://your-endpoint/v1/chat/completions
TEXT2STATA_API_KEY_LINE1=sk-...
TEXT2STATA_DEFAULT_MODEL=gpt-6.1-sol

# Stata 可执行文件（留空则自动探测）
TEXT2STATA_STATA_PATH=C:\Program Files\StataNow19\StataSE-64.exe

# 可选
TEXT2STATA_MAX_UPLOAD_MB=100
TEXT2STATA_HOST=127.0.0.1
TEXT2STATA_PORT=5000
TEXT2STATA_FLASK_DEBUG=false
```

优先级：**环境变量 > `config.json` > 内置默认值**。开发模式下 `.env` 优先级最高，网页里改的值会被它覆盖 —— 这是故意的，免得配置来源打架。

#### 启动

```bash
python launcher.py
```

自动选空闲端口、起服务、打开浏览器。也可以只起后端：

```bash
cd backend && python app.py
```

服务默认监听 `127.0.0.1:5000`。前端由后端直接托管，浏览器打开 `http://127.0.0.1:5000` 即可。旧的 `file://` 直开方式仍然可用 —— 前端会自动判断走同源还是回落到本机 5000。

> 监听地址刻意默认 `127.0.0.1` 而不是 `0.0.0.0`：这是给一个人用的桌面工具，没有理由把端口暴露到局域网。

### 试一下

仓库里的 `testdata.dta` 是一份可以直接跑的演示数据：93 家上市公司 × 14 年（2011–2024）的非平衡面板，38 个变量，含资产负债率、全要素生产率、研发投入、省级政策注意力配置等，带完整中文变量标签。

可以试着输入：「用固定效应模型跑一下政策注意力对企业全要素生产率的影响，控制资产负债率和企业规模」。

---

## 接口（本地版）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端页面（后端直接托管） |
| POST | `/api/upload` | 上传 `.dta`，返回 `file_id` 与数据 Schema |
| POST | `/api/plan` | 自然语言需求 → 结构化分析计划（不执行） |
| POST | `/api/analyze` | 提交执行任务，返回 `task_id` |
| GET | `/api/task/<task_id>` | 轮询任务状态与结果 |
| GET | `/api/download/<filename>` | 下载分析产物 |
| GET | `/api/config` | 读取当前配置（API Key 只回传掩码） |
| POST | `/api/config` | 保存配置，立即生效 |
| GET | `/api/stata/detect` | 探测本机所有 Stata 可执行文件 |
| GET | `/api/models` | 模型目录（策展清单 + 中转站 `/v1/models` 发现），供设置面板渲染下拉 |
| GET | `/api/health` | 健康检查 |

任务状态：`pending` → `running` → `success` / `failed` / `error`。

云端版接口（账号、任务、代理派发）见 `docs/PROTOCOL.md`。

---

## 测试

回归测试不依赖真实 Stata（除少数路径自检），可以直接跑：

```bash
cd backend

# 执行判定：锁死「假装成功」的各种场景（13 项）
python tests/test_execution_guard.py

# 结果校验：解析真实 Stata 日志格式（50 项）
python tests/test_result_audit.py

# 云端版接口：鉴权/任务生命周期/产物策略/属主隔离（24 项）
python tests/test_cloud_api.py

# 模型目录：策展/远程合并、非文本模型过滤、降级路径（11 项）
python tests/test_model_catalog.py
```

云端版还有一条需要本机 Stata 的全链路验证（起云端 → 起代理 → 真实执行 → 回传体检），在项目根目录跑：

```bash
python scripts/e2e_cloud.py
```

`test_result_audit.py` 里锁了几个**只在真实日志上才会暴露**的坑，它们都曾造成静默失效：

- **Stata 用省略前导零的小数**（`.1909618` 表示 0.1909618），而缺失值也是点号。按「以点开头就是缺失」判断，会让整列标准误变成 `None`，静默废掉所有标准误相关的检查。正确做法是靠 `float()` 转换失败来判缺失。
- **表头文字随命令变**：`regress` 打出 `Coef. / Std. Err.`，`xtreg` 打出 `Coefficient / std. err.`。按固定列宽写死正则会导致整张系数表抽不出来。
- **聚类数有三种写法**：`Number of clusters (id) = 5,373`（reghdfe）、`Number of groups = 5,376`（xtreg）、以及藏在脚注里的 `(Std. err. adjusted for 12 clusters in sector)`（regress）。第三种最容易漏。

改动 `stata_worker` 或 `result_auditor` 后请务必跑这两套测试。

---

## 目录结构

```
launcher.py                 启动器（本地版打包入口）
backend/                    本地版 + 分析内核（云端版复用同一套内核）
  app.py                    Flask 入口，接口与前端托管
  core/
    config.py               配置分层（环境变量 > config.json > 默认值）
    paths.py                开发/打包两种模式的路径解析
    stata_detect.py         Stata 可执行文件自动探测
    llm_agent.py            意图解析
    logic_center.py         数据结构判定与头部生成
    template_manager.py     模板注册表与脚本装配
    stata_worker.py         Stata 子进程执行与日志解析
    result_auditor.py       结果校验层
    templates/              模板库（registry.json 为唯一注册表）
  tests/                    回归测试（含云端 API 测试）
frontend/
  index.html                本地版单页前端（含设置面板）
cloud/                      云端版控制平面（Flask + SQLite）
  server.py                 账号/邀请码、任务派发、产物归档、前端托管
  db.py                     SQLite 数据层
  plans.py                  计划 → .do 装配（复用 backend 内核）
  static/index.html         云端版单页前端
agent/                      云端版本地执行代理（轮询领取 → 本机 Stata 执行 → 回传）
packaging/
  text2stata.spec           PyInstaller 配置
  build.py                  本地一键包打包脚本
scripts/
  e2e_cloud.py              云端版全链路端到端验证
docs/
  DEPLOYMENT.md             本地版 / 云端版部署方案
  PROTOCOL.md               代理 ↔ 云端接口契约
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
- 任务状态存在进程内存里，重启即丢，也没有并发与配额控制。
- 模板覆盖集中在横截面与面板模型，工具变量、断点回归、合成控制等还未纳入。
- 一键包只提供 Windows 版本。

**关于云端版**

`docs/DEPLOYMENT.md` 写了两个版本（本地 / 云端）怎么切、以及为什么这么切。

要点：Stata 的商业授权决定了「多人共享的云上 Stata」需要网络授权（太贵了T.T），所以云端版走的是**云端只做调度、执行留在用户自己机器上**的路子 —— 不用额外花钱，用户数据也不出本机。云端与代理的接口契约在 `docs/PROTOCOL.md`。

跑起来试试（云端版不需要大模型也能用，建任务走手动计划）：

```bash
# 1. 起云端（首次启动控制台会打印初始邀请码）
python -m cloud.server

# 2. 浏览器打开 http://127.0.0.1:8000，注册后在「执行代理」页创建代理、复制令牌

# 3. 本机起代理（执行 Stata 的那台机器）
python -m agent.agent init --cloud http://127.0.0.1:8000 --token t2sa_...
python -m agent.agent run

# 4. 回网页：数据集页上传 .dta → 新建任务 → 任务列表看结果
```

**值得一提**

`eval/jev_routing/` 下有一份关于「用专用路由模型替代通用大模型做意图解析」的旁路评测。结论是不建议替换（准确率净退步，延迟也没优势），但发现它在**高置信度时零错误**，可以当「不确定性探针」用。详见该目录下的 `FINDINGS.md`。

---

## 关于这份代码

本项目由一名财经院校经济学本科生借助coding agent全栈开发。开发者在四年里因课程作业、大创与毕业论文频繁使用Stata，深感它对没有编程基础的经管学生有一定门槛，而有编程基础的直接使用Python即可。同时市面上同类产品多以IDE插件形式存在，对本就用得吃力的学生来说，先学会装插件和配环境又是一道坎。

于是有了这个项目。它可能做得并不好，但如果能起到一点边际创新，那它的目的就达到了。
