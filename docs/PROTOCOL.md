# 代理 ↔ 云端接口契约

本文件是云端版（`cloud/`）与本地执行代理（`agent/`）之间的**唯一协议依据**。
两边实现都必须按本文件来，改动协议先改本文件。

设计前提（见 `docs/DEPLOYMENT.md` 第三节）：

- 云端只做控制平面：账号、意图解析、模板装配、任务派发、结果归档。
- `.dta` 数据**全程不离开用户机器**；云端只持有变量名 + 标签（schema）。
- Stata 在用户自己机器上跑，用用户自己的授权。

---

## 一、角色与鉴权

| 角色 | 鉴权方式 | 说明 |
|---|---|---|
| 用户（浏览器） | 会话 Cookie | 邀请码注册 + 密码登录。Cookie：`HttpOnly; SameSite=Lax`，HTTPS 部署时加 `Secure` |
| 代理（用户机器上的进程） | `Authorization: Bearer <agent_token>` | 令牌由用户在云端创建，**只在创建时完整返回一次**，云端只存哈希 |

代理令牌格式：`t2sa_<40 位 hex>`。云端存 `sha256(token)`，比对用哈希，不存原文。

### 首个用户怎么来

数据库为空时，云端启动会自动生成一个**初始邀请码**并打印到控制台（仅此一处可见）。
第一个注册用户自动成为管理员。此后邀请码只能由已登录用户创建。

---

## 二、任务生命周期

```
queued ──领取──> dispatched ──开始执行──> running ──回报──> succeeded / failed / error
  ▲                  │
  └──── 超时回收 ─────┘   （dispatched/running 超过 REQUEUE_AFTER 秒无回报 → 回到 queued）
```

- `queued`：已入队，等待代理领取。
- `dispatched`：已被代理领取，尚未开始执行（预留状态，当前实现领取即执行）。
- `running`：代理已开始执行。
- `succeeded`：Stata 判定执行成功（注意：这只代表"跑通了"，体检结论在 `audit` 字段）。
- `failed`：Stata 判定执行失败（带 `error_code` / `diagnosis`）。
- `error`：代理侧系统错误（找不到数据文件、Stata 启动失败等）。

回收策略：代理每次 poll 时云端顺带扫描，超时任务回到 `queued`。
默认 `REQUEUE_AFTER = 600` 秒。

---

## 三、用户侧接口

会话 Cookie 鉴权。所有响应 JSON。

### 3.1 认证

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/api/auth/register` | `{username, password, invite_code}` | 注册。邀请码错误 → 403 |
| POST | `/api/auth/login` | `{username, password}` | 登录，置会话 Cookie |
| POST | `/api/auth/logout` | — | 登出 |
| GET | `/api/auth/me` | — | 当前用户信息（含 `artifact_mode`） |
| POST | `/api/auth/invites` | `{}` | 创建邀请码（登录用户），返回 `{code}` |
| POST | `/api/auth/settings` | `{artifact_mode}` | 产物策略：`"cloud"`（回传，默认）或 `"local"`（只留本机） |

### 3.2 代理管理

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agents` `{name}` | 创建代理，**仅此一次**返回 `{agent_id, token}` |
| GET | `/api/agents` | 列出我的代理（`id, name, last_seen`，不含 token） |
| DELETE | `/api/agents/<id>` | 吊销代理。其进行中/排队中的任务直接置 `error`（任务与代理绑定，代理没了不可能再被完成） |

### 3.3 数据集（schema 注册）

数据本体在代理侧，云端只登记"哪个代理握着哪份数据的 schema"。
注册由**代理**发起（见 4.3），用户侧只读：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/datasets` | 列出我的数据集（`dataset_id, name, agent_id, schema, created_at`） |

### 3.4 任务

| 方法 | 路径 | 请求体 / 说明 |
|---|---|---|
| POST | `/api/tasks` | `{dataset_id, selected_template, variable_mapping, query?}`。Phase 1 不接大模型：模板与变量映射由调用方直接给定（"假计划"）。云端校验模板存在且 `required_globals` 齐全，装配出 `.do` 后入队 → 202 `{task_id}` |
| GET | `/api/tasks` | 任务列表（按时间倒序，含状态摘要） |
| GET | `/api/tasks/<id>` | 任务详情：状态、`do_script`、执行结果摘要、体检 `audit`、产物清单 |
| GET | `/api/tasks/<id>/log` | 完整 Stata 日志（text/plain） |
| GET | `/api/tasks/<id>/artifacts/<filename>` | 下载产物（仅 `artifact_mode=cloud` 时有实体文件） |

`variable_mapping` 的键与本地版一致：`y / x / controls / id / time` 等，
校验规则同本地版 `_missing_required_globals`。

---

## 四、代理侧接口

一律 `Authorization: Bearer <agent_token>` + JSON（产物上传除外）。
错误统一 `{"status":"error","message":...}` + 合适的状态码。令牌无效 → 401。

### 4.1 轮询领取

```
POST /api/agent/poll
{}
→ 200 {"task": null}                              # 没活
→ 200 {"task": {
        "task_id": "task_xxxx",
        "dataset_id": "ds_xxxx",
        "do_script": "use \"__T2S_DATA_PATH__\", clear\n...",
        "artifact_upload": true                   # false = 用户的 artifact_mode=local，别传产物
      }}
```

- poll 同时充当心跳（云端更新 `last_seen`）。
- 一次最多返回一个任务；云端把该任务置为 `dispatched`。
- **`do_script` 中的 `__T2S_DATA_PATH__` 是占位符**：代理执行前必须替换为
  `dataset_id` 对应的本机 `.dta` 绝对路径（用正斜杠）。云端永远不知道真实路径。
- 若代理本地找不到该 `dataset_id`（例如换了台机器装代理），按 4.2 回报
  `status=error, error_code=dataset_missing`，任务进入终态，不会反复重投。

### 4.2 回报执行结果

```
POST /api/agent/tasks/<task_id>/result
{
  "status": "success" | "failed" | "error",
  "message": "执行成功",
  "error_code": "r(198)",          # 可选，失败时
  "diagnosis": "...",              # 可选，失败时
  "time_cost": 12.3,
  "log": "<完整 Stata 日志文本>",   # 上限 2 MB，超出截断并在尾部标注
  "files": ["fe_result.rtf"]       # 本次产物文件名列表（即使 artifact_upload=false 也报名字）
}
→ 200 {"status":"ok","audit":{...}}
```

- 云端收到后把任务置为对应终态，并用结果体检层（`result_auditor`）审一遍日志与产物，
  结论存进任务的 `audit` 字段，随响应返回给代理（代理可打印给用户看）。
- 重复回报同一终态任务 → 409。

### 4.3 注册数据集

```
POST /api/agent/datasets
{"name": "testdata.dta", "schema": [{"name":"code","label":"企业代码"}, ...]}
→ 201 {"dataset_id": "ds_xxxx"}
```

代理在本地收到用户上传的 `.dta` 后调用。**只有变量名与标签，没有任何数据行。**

### 4.4 上传产物

```
POST /api/agent/tasks/<task_id>/artifacts
multipart/form-data，字段名 file，可多次调用逐文件上传
→ 201 {"stored": "fe_result.rtf", "size": 2032}
```

- 仅当 4.1 返回 `artifact_upload=true` 时才需要调用。
- **顺序要求：先传完产物，再回报结果（4.2）。** 云端在收到结果时就做结果体检，
  体检要核对产物文件，顺序反了体检只能看到日志。
- 文件名过 `secure_filename`，只允许 `.rtf/.doc/.docx/.csv/.png/.gph`，单文件 ≤ 50 MB。

---

## 五、代理本地接口（用户 ↔ 代理，127.0.0.1）

代理在本机开一个只监听 127.0.0.1 的小服务，供本机用户（或未来的前端）操作：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/datasets` | multipart 上传 `.dta`：代理存档 → 提取 schema → 调 4.3 注册 → 返回 `{dataset_id, schema}` |
| GET | `/datasets` | 本地数据集列表（`dataset_id, name, path`） |
| GET | `/status` | 代理状态：云端连通性、Stata 自检、最近任务 |

云端前端（Phase 3）上传文件时直接打代理的这个接口（浏览器 → 127.0.0.1），
因此代理要正确响应 CORS 预检：只允许云端站点域名。

---

## 六、安全要点（实现时必须遵守）

1. 用户密码用 werkzeug `generate_password_hash` 存储；代理令牌只存 sha256。
2. 会话 Cookie：`HttpOnly; SameSite=Lax`；HTTPS 部署（Phase 5）时打开 `Secure`。
3. 所有写路径校验属主：任务/数据集/代理都只能被其创建者访问。
4. 云端不持久化 BYOK 密钥（Phase 4 才引入，先立规矩）。
5. 日志上限 2 MB、产物单文件 50 MB、schema 上限 2000 个变量——防止代理异常刷爆存储。
6. 云端日志不落 `do_script` 全文与 schema 原文之外的敏感内容；变量名本身可能暴露研究对象，
   访问日志定期清理（Phase 5 细化）。
