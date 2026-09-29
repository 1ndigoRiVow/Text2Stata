# Jev 路由能力旁路评测

评估 TypeSafe AI 的 **Jev**（System One 决策模型，2026-09 发布）能否承担 Text2Stata 的 **模板路由** 这一步。

## 这个评测回答什么

Text2Stata 现在的 `parse_intent` 一次 LLM 调用干两件事：

| 子任务 | 能不能交给 Jev | 原因 |
|---|---|---|
| **模板路由**：13 个模板选 1 | 可以试 | 正是 `choice` 原语的用途；选项上限 255，模板库扩容也不会撑爆 prompt |
| **变量映射**：把 y/x/id/time 对到真实列名 | **不行** | Jev 不生成文本；映射值是 38 列 schema 的自由组合，不是固定标签集 |

所以本评测**只评路由**。

## 三条设计原则

1. **只出中文**，贴近真实用户措辞。
2. **三类题按 7:2:1 配比**：正常路由 / 歧义（应回问）/ 非分析请求（应拒绝）。
3. **期望值用集合表达**，承认多个答案都合理，不把单点答案当唯一真理。

## 跑之前

```bash
# 1. 先 dry-run，看清楚会发什么，不花钱
python run_eval.py --dry-run

# 2. 备好 key。任选其一：
#    a) jevmodel.org（有免费额度，适合评估）
#       echo "JEVMODEL_API_KEY=xxx" >> .env.local
#    b) TypeSafe 官方直连
#       echo "JEV_BASE_URL=https://jevmodel.net/v1/systemone" >> .env.local
#    方案 A 会自动复用项目根目录 .env 里的 TEXT2STATA_API_KEY_LINE1

# 3. 先拿 5 题试水，确认链路通了
python run_eval.py --arms jev --limit 5

# 4. 正式跑
python run_eval.py --arms baseline,jev
```

## 输出

`results/<时间戳>/` 下：

- `raw_jev.jsonl` / `raw_baseline.jsonl` —— 每题原始返回，便于复核与二次分析
- `report.md` —— 对比报告，含总览、分类维度、关键失败案例

## 关键指标：为什么不只看准确率

| 错误类型 | 含义 | 危害 |
|---|---|---|
| `over_route` | 题目信息不足或超出能力范围，方案却照样给了模板 | **最高**。真实使用中等于"静默产出错误结果" |
| `wrong_route` | 选错了模板 | 中。会被后续校验拦住一部分 |
| `under_route` | 该路由却拒答 | 低。体验受损，但不会产出错结果 |

做 AML 评测时那套判断在这里同样成立：**最该盯的不是"答对率"，而是"该说不知道的时候有没有装懂"。**

## 注意

- 本评测**完全旁路**：只读项目文件，不写回项目，不启动 Flask，不改 `.env`。
- 骨架 36 题，结论只作方向判断。要下正式结论需扩到 100+ 题，并 **pin 模型版本**（别名会漂移）。
- Jev 是海外服务。**评测可以，接进主线前要过数据出域审查** —— 尤其是公司合规场景。
