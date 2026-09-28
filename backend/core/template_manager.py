import os
import json
import tempfile
from pathlib import Path

class TemplateManager:
    """
    Text2Stata 模板管理器
    负责读取模板注册表 (registry.json) 并将模板与头部代码组装成可执行的 .do 脚本。
    """
    
    def __init__(self, base_dir=None):
        # 默认定位到与 template_manager.py 同级的 templates 文件夹。
        # base_dir 可显式传入（测试或替换模板库位置），
        # 避免 __main__ 自测代码误写真实的 templates/registry.json。
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent / "templates"
        self.registry_path = self.base_dir / "registry.json"
        
        # 初始化时加载注册表到内存
        self.registry = self._load_registry()

    def _load_registry(self):
        """内部方法：加载 JSON 注册表"""
        if not self.registry_path.exists():
            raise FileNotFoundError(f"找不到模板注册表：{self.registry_path}\n请确保创建了该文件！")
            
        with open(self.registry_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_template_info(self, template_name):
        """获取单个模板的元数据信息"""
        if template_name not in self.registry:
            raise ValueError(f"未知的模板名称：'{template_name}'，请检查 registry.json")
        return self.registry[template_name]

    def get_all_templates(self):
        """获取所有可用模板的列表（未来可用于前端下拉菜单或交给大模型参考）"""
        return self.registry

    def assemble_script(self, template_name, header_code):
        """
        核心装配流水线：将生成的 global 头部与预设的 .do 模板拼接。
        """
        # 1. 获取模板信息
        template_info = self.get_template_info(template_name)
        
        # 2. 定位 .do 文件实际路径
        do_file_rel_path = template_info.get("file_path")
        do_file_abs_path = self.base_dir / do_file_rel_path
        
        if not do_file_abs_path.exists():
            raise FileNotFoundError(f"找不到模板对应的 .do 文件：{do_file_abs_path}")
            
        # 3. 读取 .do 模板内容
        with open(do_file_abs_path, 'r', encoding='utf-8') as f:
            template_code = f.read()
            
        # 4. 组装最终脚本 (头部 + 换行 + 模板代码)
        final_script = f"{header_code}\n\n{template_code}"
        
        return final_script


# ==========================================
# 本地测试模块 (直接运行此文件可看效果)
# ==========================================
if __name__ == "__main__":
    # 注意：自测一律写入系统临时目录。
    # 绝不能写进 templates/，否则会用测试用的 1 个模板覆盖真实的 registry.json。
    test_base = Path(tempfile.mkdtemp(prefix="t2s_template_selftest_"))
    test_base.mkdir(exist_ok=True)
    (test_base / "regress").mkdir(exist_ok=True)
    
    # 动态写入一个 registry.json 用于测试
    test_registry = {
        "ols_basic": {
            "name": "基础 OLS 回归",
            "file_path": "regress/ols_basic.do"
        }
    }
    with open(test_base / "registry.json", "w", encoding="utf-8") as f:
        json.dump(test_registry, f, ensure_ascii=False, indent=4)
        
    # 动态写入一个 ols_basic.do 用于测试
    test_do_code = """* 模板：基础 OLS 回归
* 执行回归并输出稳健标准误
regress $y $x $controls, vce(robust)
est store model_ols
esttab model_ols using "ols_result.rtf", replace
"""
    with open(test_base / "regress" / "ols_basic.do", "w", encoding="utf-8") as f:
        f.write(test_do_code)

    print(f"✅ 已在临时目录创建测试模板库：{test_base}\n")

    # ---------------- 模拟流水线执行 ----------------
    print("=== 开始组装测试 ===")
    
    # 1. 模拟 logic_center.py 传过来的头部代码
    mock_header = """* ==========================================
* Text2Stata 自动生成 - 变量定义与环境设置
* ==========================================
global y "salary"
global x "education"
global controls "age gender city"
* 当前识别为横截面数据，无需 xtset/tsset
* =========================================="""

    # 2. 实例化管理器并执行组装（显式指向临时模板库，不触碰真实模板）
    try:
        manager = TemplateManager(base_dir=test_base)
        final_do_file_content = manager.assemble_script("ols_basic", mock_header)
        
        print("🎉 组装成功！最终将被发送给 Stata 执行的代码如下：\n")
        print("-------------------------------------------------")
        print(final_do_file_content)
        print("-------------------------------------------------")
        
    except Exception as e:
        print(f"❌ 发生错误: {e}")