"""
Text2Stata 核心逻辑处理中心
负责处理变量映射、数据结构判定，并生成 Stata 代码的执行头部 (Header)
"""

def determine_data_structure(id_var=None, time_var=None):
    """
    根据前端传回的个体标识和时间变量，判定数据结构类型。
    """
    # 转换为布尔值，处理空字符串、None等情况
    has_id = bool(id_var and str(id_var).strip())
    has_time = bool(time_var and str(time_var).strip())

    if not has_id and not has_time:
        return "cross_sectional"  # 横截面数据
    elif not has_id and has_time:
        return "time_series"      # 时间序列数据
    elif has_id and has_time:
        return "panel"            # 面板数据
    else:
        # 只有 ID 没有时间，通常按横截面或聚类横截面处理
        return "cross_sectional" 

def generate_stata_header(var_mapping):
    """
    根据前端传回的变量映射字典，生成 Stata 脚本的 global 头部。
    
    参数示例 var_mapping:
    {
        'y': 'gdp',
        'x': 'fdi',
        'controls': ['pop', 'edu', 'export'], # 支持列表形式，自动用空格拼接
        'id': 'province_code',
        'time': 'year'
    }
    """
    header_lines = []
    header_lines.append("* ==========================================")
    header_lines.append("* Text2Stata 自动生成 - 变量定义与环境设置")
    header_lines.append("* ==========================================\n")
    
    header_lines.append("* 1. 核心变量全局声明")
    
    # 提取特殊的 id 和 time 变量用于后续判定
    id_var = var_mapping.get('id', '')
    time_var = var_mapping.get('time', '')
    
    # 遍历字典，动态生成 global 声明
    for key, value in var_mapping.items():
        if not value:  # 跳过未填写的空槽位
            continue
            
        if isinstance(value, list):
            # 如果是控制变量列表，用空格连接拼成一个字符串
            val_str = " ".join([str(v).strip() for v in value if v])
            if val_str:
                header_lines.append(f'global {key} "{val_str}"')
        else:
            # 单个变量直接声明
            header_lines.append(f'global {key} "{str(value).strip()}"')
            
    # 2. 数据结构判定与高级设定
    header_lines.append("\n* 2. 数据结构声明与设定")
    data_structure = determine_data_structure(id_var, time_var)
    
    if data_structure == "panel":
        # 用 capture 包住：个体-时间组合有重复时 xtset 会报 r(451)，
        # 而这时恰恰需要让清洗类模板跑起来去重（2026-10-22 实测：
        # deduplicate 自己都被 header 的 xtset 打死了，等于没有自救通道）。
        # xtset 失败不阻断，但要把原因和出路讲清楚——
        # 真正的面板命令（xtreg / reghdfe）随后会正常报"必须先 xtset"。
        header_lines.append("capture xtset $id $time  // 面板数据设定")
        header_lines.append("if _rc {")
        header_lines.append('    di ""')
        header_lines.append('    di "⚠ 面板设定失败：$id 与 $time 的组合不唯一（存在重复观测）。"')
        header_lines.append('    di "  后续 xtreg / reghdfe 等面板命令会报错。"')
        header_lines.append('    di "  先跑一次「重复观测处理」，或把数据 collapse 到个体-时间层面。"')
        header_lines.append('    di ""')
        header_lines.append("}")
    elif data_structure == "time_series":
        header_lines.append(f"tsset $time  // 时间序列设定")
    else:
        header_lines.append("* 当前识别为横截面数据，无需 xtset/tsset")
        
    header_lines.append("\n* ==========================================")
    header_lines.append("* 代码执行区开始")
    header_lines.append("* ==========================================\n")
    
    # 返回拼接好的完整字符串，并附带数据结构类型，方便后续路由模板
    return {
        "header_code": "\n".join(header_lines),
        "data_structure": data_structure
    }


# ==========================================
# 本地测试模块 (直接运行此文件可看效果)
# ==========================================
if __name__ == "__main__":
    print("=== 测试场景 1：前端传来了面板数据 (例如双重差分) ===")
    mock_panel_request = {
        'y': 'ROE',
        'x': 'ESG_score',
        'controls': ['Size', 'Lev', 'Cash', 'Age'],
        'id': 'Stkcd',
        'time': 'Year',
        'iv': ''  # 没有填工具变量
    }
    
    result1 = generate_stata_header(mock_panel_request)
    print(f"识别出的数据结构: {result1['data_structure']}\n")
    print(result1['header_code'])
    
    print("\n\n=== 测试场景 2：前端传来了横截面数据 (例如普通OLS) ===")
    mock_cross_request = {
        'y': 'wage',
        'x': 'education',
        'controls': ['experience', 'gender', 'city_tier'],
        'id': '',
        'time': ''
    }
    
    result2 = generate_stata_header(mock_cross_request)
    print(f"识别出的数据结构: {result2['data_structure']}\n")
    print(result2['header_code'])