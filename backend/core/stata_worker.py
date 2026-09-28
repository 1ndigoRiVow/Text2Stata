import os
import subprocess
import re
from pathlib import Path
import time
import uuid

class StataWorker:
    """
    Text2Stata 核心执行引擎
    负责在后台调用 Stata 命令行，执行 .do 脚本，并捕获日志和报错。
    """
    
    def __init__(self, stata_path=None):
        # 1. 确定工作目录 (用于存放临时的 do 文件和 log 文件)
        self.workspace = Path(__file__).parent.parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        
        # 2. 配置 Stata 执行文件路径
        # 如果没有传入，系统会尝试使用环境变量中的默认命令
        # Windows 用户通常需要填具体路径，如 "C:/Program Files/Stata17/StataMP-64.exe"
        # Mac/Linux 用户通常是 "stata-mp" 或 "stata"
        self.stata_path = stata_path or self._detect_default_stata()

    def _detect_default_stata(self):
        """简单探测系统默认的 stata 命令 (这是一个 fallback 机制)"""
        if os.name == 'nt': # Windows
            return "StataMP-64.exe" # 假设环境变量里有
        else: # Mac/Linux
            return "stata-mp"

    def execute_script(self, do_file_content, task_id=None):
        """
        核心执行方法
        :param do_file_content: 完整的 Stata 代码字符串
        :param task_id: 任务唯一标识，如果不传则自动生成
        :return: 字典包含执行状态、日志内容、报错信息等
        """
        # 生成唯一任务ID，避免并发时文件覆盖
        if not task_id:
            task_id = f"task_{uuid.uuid4().hex[:8]}"
            
        do_file_path = self.workspace / f"{task_id}.do"
        log_file_path = self.workspace / f"{task_id}.log"
        
        # 1. 将代码写入 .do 文件
        with open(do_file_path, "w", encoding="utf-8") as f:
            f.write(do_file_content)
            
        # 2. 构建命令行指令
        # -e: batch mode (静默执行，不弹窗)
        # -q: suppress logo (跳过启动 logo 加快速度)
        # do: 执行 do 文件
        cmd = [self.stata_path, "-e", "-q", "do", str(do_file_path)]
        
        start_time = time.time()
        
        try:
            # 3. 阻塞式执行 Stata 进程，设置超时时间防卡死 (比如 5 分钟)
            # 注意：cwd 设置为 workspace，这样 Stata 生成的各种图表/表格默认也会保存在这里
            process = subprocess.run(
                cmd, 
                cwd=str(self.workspace),
                capture_output=True, 
                text=True, 
                timeout=300 
            )
            
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "执行超时 (超过 300 秒)", "error_code": "timeout"}
        except FileNotFoundError:
             return {"status": "error", "message": f"找不到 Stata 启动程序，请检查路径: {self.stata_path}"}
        except Exception as e:
            return {"status": "error", "message": f"系统未知错误: {str(e)}"}

        execution_time = time.time() - start_time
        
        # 4. 解析日志文件
        log_content, error_code = self._parse_log_file(log_file_path)
        
        # 🌟 [新增代码] 扫描工作区，寻找生成的成果文件 (RTF, DOC, DOCX 等)
        generated_files = []
        for file in os.listdir(self.workspace):
            if file.endswith(('.rtf', '.doc', '.docx', '.csv', '.png', '.gph')):
                # 注意：这里我们简单地把当前目录下的成果文件都算作这次任务的
                # 生产环境中可以用 task_id 作为文件名前缀来严格隔离
                generated_files.append(file)
                
        # 5. 返回结构化结果，加入 generated_files
        if error_code:
            return {
                "status": "failed",
                "task_id": task_id,
                "error_code": error_code,
                "message": "Stata 执行报错，请检查代码或数据。",
                "log": log_content,
                "time_cost": round(execution_time, 2),
                "files": generated_files # 🌟 新增
            }
        else:
            return {
                "status": "success",
                "task_id": task_id,
                "message": "执行成功",
                "log": log_content,
                "time_cost": round(execution_time, 2),
                "files": generated_files # 🌟 新增
            }

        

    def _parse_log_file(self, log_file_path):
        """读取日志，并使用正则探测是否包含 r(***) 报错信息"""
        if not log_file_path.exists():
            return "未生成日志文件", "log_missing"
            
        with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
            log_content = f.read()
            
        # Stata 报错的标准格式通常是： r(198); 或者 r(2000);
        # 我们用正则表达式去抓取它
        error_match = re.search(r'r\(([0-9]+)\);?', log_content)
        error_code = f"r({error_match.group(1)})" if error_match else None
        
        return log_content, error_code


# ==========================================
# 本地测试模块 
# ==========================================
if __name__ == "__main__":
    print("=== 开始测试 Stata 执行引擎 ===\n")
    
    LOCAL_STATA_PATH = r"C:\Program Files\StataNow19\StataSE-64"  # 如果你配置了环境变量，可以用这个
    
    worker = StataWorker(stata_path=LOCAL_STATA_PATH)
    
    # 准备一段安全的测试代码 (调用 Stata 自带的 auto 数据集)
    test_do_code = """
    sysuse auto, clear
    sum price mpg rep78
    regress price mpg rep78, robust
    """
    
    print(f"当前工作区: {worker.workspace}")
    print("正在呼叫 Stata 执行回归测试...\n")
    
    result = worker.execute_script(test_do_code)
    
    print(f"执行状态: {result['status']}")
    print(f"耗时: {result.get('time_cost', 0)} 秒")
    
    if result['status'] == 'success':
        print("\n✅ 执行成功！Stata 日志最后 500 个字符：")
        print("-------------------------------------------------")
        print(result['log'][-500:])
        print("-------------------------------------------------")
    else:
        print(f"\n❌ 执行失败！报错代码: {result.get('error_code')}")
        print(f"错误信息: {result.get('message')}")
        if result.get('log'):
            print("\n日志内容：")
            print(result['log'])
