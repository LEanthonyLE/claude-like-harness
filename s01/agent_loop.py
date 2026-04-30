import os
import subprocess

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

client = OpenAI(
    base_url = os.getenv("OPENROUTER_BASE_URL"),
    api_key = os.getenv("OPENROUTER_API_KEY")
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
    }
}]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout(120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

## 核心agent loop
def agent_loop(messages: list):
    while True:
        response = client.chat.completions.create(
            model = os.environ["MODEL_ID"],
            messages = messages,
            tools = TOOLS,
            max_tokens = 8000,
            extra_body = {"reasoning": {"enable": True}}
        )
        # print(response)
        assistan_message = response.choices[0].message
        messages.append(assistan_message)
        tool_calls = assistan_message.tool_calls
        if not tool_calls:
            return assistan_message.content
        
        for tool_call in tool_calls:
            if tool_call.function.name == "bash":
                import json
                args = json.loads(tool_call.function.arguments)
                command = args.get("command")
                print(f"\033[33m$ {command}\033[0m")
                output = run_bash(command)
                print(output[:200])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": "bash",
                    "content": output,
                })

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
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
                if hasattr(block, "text"):
                    print(block.text)
        elif isinstance(response_content, str):
            print(response_content)
        
        print()