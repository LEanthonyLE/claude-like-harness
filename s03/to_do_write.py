import os
import subprocess
from pathlib import Path


## 终端打字
try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')

except ImportError:
    pass
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("OPENROUTER_API_KEY"):
    os.environ.pop("OPENROUTER_AUTH_TOKEN", None)

WORKDIR = Path.cwd()


client = OpenAI(
    base_url = os.getenv("OPENROUTER_BASE_URL"),
    api_key = os.getenv("OPENROUTER_API_KEY")
)

SYSTEM = f"You are a coding agent at {WORKDIR} on a windows system, \
Use Windows CMD commands (like 'dir' instead of 'ls', 'type' instead of 'cat') when using the cmd tool. \
MUST Use the todo tool to plan multi-step tasks(2 or more steps). Mark in_progress before starting, completed when done. \
Use tools to solve tasks. Act, don't explain." 

class TodoManager:
    def __init__(self):
        self.items = []

    ## 初始化item内容，只允许单一in_process
    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 tools allowed")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_process", "completed"):
                raise ValueError(f"Item {item_id}: invalid status")
            if status == "in_process":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_process at a time")
        self.items = validated
        return self.render()
    
    ## 返回内容可视化
    def render(self) -> str:
        if not self.items:
            return "No todos"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_process": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n{done}/{len(self.items)} completed")
        return "\n".join(lines)

TODO = TodoManager()



### --------------------- tools implementations ------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "cmd",
                "description": "Run a windows cmd command in the workspace",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            

        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read a file's content",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["path"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "write content to file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "replace a files's exact content",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                "required": ["path", "old_text", "new_text"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": "Update task list. Track progress on multi-step tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {   # 属性名
                        "type": "array", 
                        "items":{               # 每一项items包含内容
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"}, 
                                "text": {"type": "string"}, 
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_process", "completed"],
                                },
                            },
                            "required": ["id", "text", "status"]
                        },
                    },
                }, 
                "required": ["items"],
            },
        },
    },

]
## dispatch map   
# 取消if else的工具对齐
TOOL_HANDLERS = {
    "cmd":         lambda **kw: run_cmd(kw["command"]),
    "read_file":    lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":         lambda **kw: TODO.update(kw["items"])
}

def safe_path(p: str) -> Path:
    # 当前路径与安全工作路径拼接并解析为绝对路径
    path = (WORKDIR / p).resolve()
    # 判断是否在安全工作路径及其子目录内
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_cmd(command: str) -> str:
    dangerous = [ "del ", "erase ", "rmdir ", "rd ", "format ",]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=False, timeout=120,
                           )
        
        def decode_output(binary_data):
            for encoding in ['utf-8', 'gbk', 'cp936']:
                try:
                    return binary_data.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return binary_data.decode('utf-8', errors='replace')
        stdout = decode_output(r.stdout)
        stderr = decode_output(r.stderr)
        out = (stdout + stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    
def run_read(path: str, limit: int = None) -> str:
    try:
        # 读取文件内容
        # 读文件的命令输入是utf-8但是执行过程用了gbk,指定encoding
        text = safe_path(path).read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        if limit and limit < len(lines): # 限制行数
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000] # 限制字符数，避免单行字数过多
    except Exception as e:
        return f"Error: {e}"
    
def run_write(path: str, content: str) -> str:
    try:
        sp = safe_path(path)
        # 自动创建不存在的目录或文件
        sp.parent.mkdir(parents = True, exist_ok = True)
        sp.write_text(content)
        return f"wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        sp = safe_path(path)
        content = sp.read_text(encoding='utf-8', errors='replace')
        if old_text not in content:
            return f"Error: Text not found in {path}"
        sp.write_text(content.replace(old_text, new_text, 1)) # 只替换到第一个匹配到的old_text
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"




### --------------------- agent loop with nag reminder injection ------------------

## 核心agent loop
def agent_loop(messages: list):
    rounds_since_todo = 0
    while True:
        response = client.chat.completions.create(
            model = os.environ["MODEL_ID"],
            messages = [{"role": "system", "content": SYSTEM}] + messages,
            tools = TOOLS,
            max_tokens = 8000,
            extra_body = {"reasoning": {"enable": True}}
        )
        # print(response)
        assistan_message = response.choices[0].message
        reason_message = getattr(assistan_message, "reasoning", None)
        if reason_message:
            print(f"\033[90m[think]: {reason_message}\033[0m")
        messages.append(assistan_message)
        tool_calls = assistan_message.tool_calls
        if not tool_calls:
            return assistan_message.content
        
        use_todo = False


        for tool_call in tool_calls:
            import json
            ## openai sdk arguments 是一个json字符串需要转换为字典作为**传入参数
            kw_dict = json.loads(tool_call.function.arguments)
            handler = TOOL_HANDLERS.get(tool_call.function.name)
            output = handler(**kw_dict) if handler else f"Unknown tool: {tool_call.function.name}"  #  **将字典拆解为关键字参数传入handler 对应kw关键字参数
            print(f"> {tool_call.function.name}")
            # tools return
            print(output[:200])
            messages.append({"role": "tool", "tool_use_id": tool_call.id, "name": tool_call.function.name, "content": str(output)})
            if tool_call.function.name == "todo":
                use_todo = True

        # 记录todo未使用轮数，超过三轮进行消息提醒
        rounds_since_todo = 0 if use_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            messages.append({"role": "reminder", "text": "<reminder>Update your todos.</reminder>"})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "quit", "exit", ""):
            break
        # 保存的是对象
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1].content
        # content包含resoning的条目情况下
        if isinstance(response_content, list):
            for block in response_content:
                # 可能多模态情况下返回image_url和text
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        print(block.text)
                else:
                    if hasattr(block, "text"):
                        print(block.text)
        elif isinstance(response_content, str):
            print(response_content)
        
        print()