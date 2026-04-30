import os
import subprocess
from pathlib import Path
import json
import time
import yaml
import re

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
SKILLS_DIR = WORKDIR / "skills"

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3
PRESAVE_RESULT_TOOLS = {"write_file"}

TASKS_DIR = WORKDIR / ".tasks"


client = OpenAI(
    base_url = os.getenv("OPENROUTER_BASE_URL"),
    api_key = os.getenv("OPENROUTER_API_KEY")
)

def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    return len(str(messages)) // 4

# -- Layer 1: micro_compact - replace old tool results with placeholders --
# 一阶段的工具结果压缩，最早的三条长工具调用信息压缩掉
def micro_compact(messages: list) -> list:
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    tool_results = []
    ## 此时包含对象和字典，所以需要统一处理成确定格式才能进行下一步的识别操作（必须）
    for msg_idx, msg in enumerate(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "tool":
            tool_results.append((msg_idx, msg))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    
    # 创建工具id与name键值对
    tool_name_map = {}
    for msg in messages:
        # 字典情况
        if isinstance(msg, dict):
            role = msg.get("role")
            if role == "tool" and msg.get("name"):
                tool_name_map[msg.get("tool_call_id")] = msg["name"]

        else:
            role = getattr(msg, "role", None)
            if role == "tool" and getattr(msg, "name", None):
                tool_name_map[msg.get("tool_call_id")] = msg["name"]
    to_clear = tool_results[:-KEEP_RECENT]
    for msg_idx, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_call_id", str)
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESAVE_RESULT_TOOLS:
            continue
        # 替换过程，直接替换成简短的消息，使用某某工具，具体结果忽略
        result["content"] = f"[Previous: used{tool_name}]"
    return messages

# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
def auto_compatct(messages: list) -> list:
    # Save full transcript to disk
    # 先把消息保存到jsonl文件
    TRANSCRIPT_DIR.mkdir(exist_ok = True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default = str) + "\n")
        print(f"[transcipt saved: {transcript_path}]")
    # 大模型总结信息
    conversation_text = json.dumps(messages, default = str)[-80000:]
    response = client.chat.completions.create(
        model = os.environ["MODEL_ID"],
        messages = [{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens = 2000,
        extra_body = {"reasoning": {"enable": True}}
    )
    summary = next((block for block in response.choices[0].message.content if hasattr(block, "text")), "")
    if not summary:
        summary = "No summary generated."
    return [
         {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    ]

class Skillloader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()
    def _load_all(self):
        if not self.skills_dir.exists():
            return
        # 查找md文件路径
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}
    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        """解析元数据（文档前面---分块部分）和整体的skill内容"""
        # 返回一个match对象group（1）（2）
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()
    
    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        if not self.skills:
            return "(no available skills)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"   -{name}: {desc}"
            if tags:
                line += f" [t{tags}]"
            lines.append(line)
        return "\n".join(lines)
    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name-\"{name}\">\n{skill['body']}\n</skill>"


SKILL_LOADER = Skillloader(SKILLS_DIR)


SYSTEM = f"You are a coding agent at {WORKDIR} on a windows system, \
Use Windows CMD commands (like 'dir' instead of 'ls', 'type' instead of 'cat') when using the cmd tool. \
Use tools to solve tasks. Act, don't explain. \
Skills available: \
{SKILL_LOADER.get_descriptions()} \
Use load_skill to access specialized knowledge before tackling unfamiliar topics." 

SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR} on a windows system. Complete the given task, then summarize your findings. \
MUST Use the todo tool to plan multi-step tasks(2 or more steps). Mark in_progress before starting, completed when done. \
Use tools to solve tasks. Act, don't explain."

class TaskManger:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1
    # 只需开始时获取一次max，之后next自加
    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0
    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"task {task_id} not found")
        # json 转 dict
        return json.loads(path.read_text())
    def _save(self, tasks: dict):
        path = self.dir / f"task_{tasks['id']}.json"
        # indent 缩进空格 ， ensure_ascii不需要转义中文为特殊编码
        path.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))
    # 需要llm自己规划进参
    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "owner": "",
        }
        self._save(task) # 保存到硬盘持久化
        self._next_id += 1
        # 返回给调用层
        return json.dumps(task, indent=2, ensure_ascii=False)
    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)
    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_process", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            # blockedBy本身是个list, set() 集合去重操作， 最终转换为列表赋值 
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)
    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)
    def list_all(self) -> str:
        tasks = []
        files = sorted(
            self.dir.glob("task_*.json"),
            key = lambda f: int(f.stem.split("_")[1])
        )
        for f in files:
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "no tasks"
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_process": "[>]", "completed": "[x]"}.get(t["status"],"[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)
TASKS = TaskManger(TASKS_DIR)

### --------------------- tools implementations ------------------
CHILD_TOOLS = [
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
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load specialized knowledge by name.",
            "parameters": {
                "type": "object",
                "properties": {"name":{"type": "string", "description": "skill name to load"}},
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compact",
            "description": "Trigger manual conversation compression.",
            "parameters": {
                "type": "object",
                "properties": {"foucus":{"type": "string", "description": "What to preserve in the summary"}},

            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a new task.",
            "parameters": {
                "type": "object",
                "properties": {"subject":{"type": "string"}, "description": {"type": "string"}},
                "required": ["subject"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update a task's status or dependencies.",
            "parameters": {
                "type": "object",
                "properties": {"task_id":{"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "removeBlockedBy": {"type": "array", "items": {"type": "integer"}}},
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List all tasks with status summary.",
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_get",
            "description": "Get full details of a task by ID.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"]
            }
        }
    },

]
## dispatch map   
# 取消if else的工具对齐
TOOL_HANDLERS = {
    "cmd":          lambda **kw: run_cmd(kw["command"]),
    "read_file":    lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill":   lambda **kw: SKILL_LOADER.get_content(kw['name']),
    "compact":      lambda **kw: "Manual compression requested.",
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("removeBlockedBy")),
    "task_list":   lambda **kw: TASKS.list_all(),
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
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


### --------------------- subagent ------------------
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    for _ in range(30):
        response = client.chat.completions.create(
            model =  os.environ["MODEL_ID"],
            messages = [{"role": "system", "content": SYSTEM}] + sub_messages,
            tools = CHILD_TOOLS,
            max_tokens = 8000,
            extra_body = {"reasoning": {"enable": True}}
        )
        msg = response.choices[0].message
        sub_messages.append({"role": "assistant", "content": msg.content})
        tool_calls = msg.tool_calls
        if getattr(msg, 'reasoning', None):
            print(f"\033[90m[Sub-Thought]: {msg.reasoning}\033[0m")
        if not tool_calls:
            break
        
        for tool_call in tool_calls:
            
            kw_dict = json.loads(tool_call.function.arguments)
            handler = TOOL_HANDLERS.get(tool_call.function.name)
            output = handler(**kw_dict) if handler else f"Unknown tool: {tool_call.function.name}"  #  **将字典拆解为关键字参数传入handler 对应kw关键字参数
            print(f"> {tool_call.function.name}")
            # tools return
            print(output[:200])
            sub_messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": str(output)})
            # Only the final text returns to the parent -- child context is discarded
    return "".join(msg.content) or "(no summary)"

PARENT_TOOLS = CHILD_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "pawn a subagent with fresh context. It shares the filesystem but not conversation history." ,
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "Short description of the task"
                    },
                    "required": ["prompt"]
                }

            }
        }
    }
]



### --------------------- agent loop with nag reminder injection ------------------

## 核心agent loop
def agent_loop(messages: list):
    rounds_since_todo = 0
    while True:
        micro_compact(messages)
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compatct(messages)
        response = client.chat.completions.create(
            model = os.environ["MODEL_ID"],
            messages = [{"role": "system", "content": SYSTEM}] + messages,
            tools = PARENT_TOOLS,
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
        manual_compact = False
        
        for tool_call in tool_calls:
            if tool_call.function.name == "task":
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                desc = args.get("description", "subtask")
                prompt = args.get("prompt", "")
                print(f"> task ({desc}): {prompt[:80]}...")
                output = run_subagent(prompt)
            elif tool_call.function.name == "compact":
                manual_compact = True
                output = output = "Compressing..."
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": str(output)})
            else:
                ## openai sdk arguments 是一个json字符串需要转换为字典作为**传入参数
                kw_dict = json.loads(tool_call.function.arguments)
                handler = TOOL_HANDLERS.get(tool_call.function.name)
                output = handler(**kw_dict) if handler else f"Unknown tool: {tool_call.function.name}"  #  **将字典拆解为关键字参数传入handler 对应kw关键字参数
                print(f"> {tool_call.function.name}")
                # tools return
                print(output[:200])
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": str(output)})
                if tool_call.function.name == "todo":
                    use_todo = True

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compatct(messages)
            return 
        # 记录todo未使用轮数，超过三轮进行消息提醒
        rounds_since_todo = 0 if use_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            messages.append({"role": "reminder", "text": "<reminder>Update your todos.</reminder>"})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
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