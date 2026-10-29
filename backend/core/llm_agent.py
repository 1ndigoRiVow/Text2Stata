import json

import requests

from core import config


# 可选模型与推荐默认值统一放在 core/model_catalog.py，不要在这里再硬编码一份：
# 两份列表迟早会漂，而且默认值一旦漂移，用户每次新建任务都得手动改。


class LLMAgent:
    """
    Intent parser for Text2Stata.

    It converts a natural language request plus dataset schema into a strict
    JSON analysis plan that the deterministic template engine can execute.
    """

    def __init__(self, current_line="line1", current_model=None, api_url=None, api_key=None):
        # 走 config 的属性访问而不是 import 常量：
        # 设置面板改完 key/模型后要立刻生效，不能用模块导入时的那份旧快照。
        self.api_url = api_url or config.API_URL
        self.max_tokens = 8000
        self.timeout = 120
        # 上一次调用的 token 用量（云端计量用）；本地版不看它，留着无害
        self.last_usage = None
        if api_key:
            # 显式注入密钥：不查 config 的线路表。云端按请求带 BYOK 时走这条，
            # 密钥只在内存里活一次，不碰 config、不落盘。
            self.current_line_name = "explicit"
            self.api_key = api_key
        else:
            self.switch_line(current_line)
        self.switch_model(current_model or config.DEFAULT_MODEL)

    def switch_line(self, line_name):
        key = config.API_LINES.get(line_name, "")
        if key:
            self.current_line_name = line_name
            self.api_key = key
            return
        raise ValueError(
            f"API 线路「{line_name}」还没有配置密钥。请在设置面板里填入，"
            f"或设置环境变量 TEXT2STATA_API_KEY_{line_name.upper()}。"
        )

    def switch_model(self, model_name):
        self.model = model_name or config.DEFAULT_MODEL

    def parse_intent(self, user_query, schema_info, template_registry):
        system_prompt = f"""
You are a professional econometrics assistant and the planning engine of Text2Stata.
Your job is to convert a user's natural language analysis request into strict JSON.

Available dataset variables:
{json.dumps(schema_info, ensure_ascii=False)}

Available Stata template registry:
{json.dumps(template_registry, ensure_ascii=False, indent=2)}

Output requirements:
Return valid JSON only. Do not include markdown fences or explanatory text outside JSON.
The JSON must contain:
1. "selected_template": string. It must be one key from the template registry.
2. "variable_mapping": object. Available roles:
     "y" (outcome), "x" (main regressor), "controls" (array of controls),
     "id" (panel unit), "time" (period),
     "iv" (instrument, for IV-2SLS), "m" (mediator, for mediation),
     "policy" (period the treatment starts, for event study; a variable or a bare year),
     "treat" (treatment group dummy).
   Fill only the roles declared in the chosen template's required_globals / optional_globals.
   Use an empty string or empty array when a field is not mentioned.
3. "reasoning": string. Briefly explain why this template and variable mapping were chosen.
"""

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"用户需求: {user_query}"},
            ],
            "temperature": 0.1,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

        response = None
        result_str = ""
        self.last_usage = None
        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()

            result_data = response.json()
            self.last_usage = result_data.get("usage") or None
            result_str = result_data["choices"][0]["message"]["content"].strip()
            if result_str.startswith("```json"):
                result_str = result_str[7:]
            if result_str.endswith("```"):
                result_str = result_str[:-3]

            return json.loads(result_str.strip())

        except requests.exceptions.Timeout:
            return {
                "status": "error",
                "message": f"大模型请求超时，已超过 {self.timeout} 秒。请稍后重试或切换更小的模型。",
            }
        except requests.exceptions.RequestException as exc:
            error_detail = str(exc)
            if response is not None:
                try:
                    error_detail = response.json().get("error", {}).get("message", str(exc))
                except Exception:
                    pass
            return {"status": "error", "message": f"API 网络请求失败: {error_detail}"}
        except json.JSONDecodeError as exc:
            return {
                "status": "error",
                "message": f"大模型返回了非标准 JSON: {exc}\n原始返回: {result_str}",
            }
        except KeyError as exc:
            return {"status": "error", "message": f"API 返回数据结构异常: {exc}"}
        except Exception as exc:
            return {"status": "error", "message": f"意图解析失败: {exc}"}


if __name__ == "__main__":
    agent = LLMAgent()
    print(f"line={agent.current_line_name}, model={agent.model}")
