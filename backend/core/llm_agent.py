import json

import requests

from core.config import API_LINES, API_URL, DEFAULT_MODEL


AVAILABLE_MODELS = [
    "gpt-3.5-turbo",
    "gpt-4o-mini",
    "gpt-4o",
    "deepseek-v3.2",
]


class LLMAgent:
    """
    Intent parser for Text2Stata.

    It converts a natural language request plus dataset schema into a strict
    JSON analysis plan that the deterministic template engine can execute.
    """

    def __init__(self, current_line="line1", current_model=None):
        self.api_url = API_URL
        self.max_tokens = 8000
        self.timeout = 120
        self.switch_line(current_line)
        self.switch_model(current_model or DEFAULT_MODEL)

    def switch_line(self, line_name):
        if line_name in API_LINES and API_LINES[line_name]:
            self.current_line_name = line_name
            self.api_key = API_LINES[line_name]
            return
        env_name = f"TEXT2STATA_API_KEY_{line_name.upper()}"
        raise ValueError(f"API line '{line_name}' is not configured. Set {env_name} in .env.")

    def switch_model(self, model_name):
        self.model = model_name or DEFAULT_MODEL

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
2. "variable_mapping": object. Include "y", "x", "controls", "id", "time".
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
        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()

            result_data = response.json()
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
