import os 
import subprocess
import json
from pathlib import Path
import re 
from anthropic import Anthropic
from dotenv import load_dotenv
import anthropic
import time

load_dotenv(override=True)
client=Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model=os.getenv("MODEL_ID")

WORKDIR=Path.cwd()
ENVIRONMENT_PROMPT=(
    "Windose:use bash toll cmd.exe ,not unix"
    if os.name=='nt'
    else "unix like : bash tool turn on using unix"

)


SYSTEM=(
    f"you are coding agent working on {WORKDIR},Environment {ENVIRONMENT_PROMPT}"
    "use tools to solve task ,do not use permission yourself"
    "the harness will approve or deny before execution"
    " Do not repeat an identical failed command"
    "After two failed attempts using the same approach"
    "try a different strategy"
    """When answering informational questions:
- Once sufficient evidence is available, provide the answer
- Avoid unnecessary repeated tool calls.
- Do not prioritize temporary-file cleanup over answering."""
)


Tools=[{
 "name":'bash',
 "description":"run bash commond",
 "input_schema":{
    'type':"object",
    "properties":{"command": {"type":"string"} },
    "required":["command"]
}}
,
{'name':"read_file",
"description":"read the file contents",
"input_schema":{

    "type":"object",
    "properties":{"path":{"type":"string"},"limit":{'type':"integer"}},
    "required":['path']
}},

{"name":"write_file",
"description":"write contents",
"input_schema":{
    "type":'object',
    "property":{"path":{"type":"string"},"content":{"type":"string"}},
    "required":["path","content"]
}},

{
"name":"edit_file",
"description":"Replace exact text in to the file ",
"input_schema":{
    'type':'object',
    "properties":{"path":{'type':'string'},"old_text":{"type":'string'},"new_text":{"type":"string"}},
     "required":["path","old_text","new_text"]
}
},


{"name":"global",
"description":'find files using glob pattern',
"input_schema":{
    "type":"object",
    "properties":{"pattern":{'type':"string"}},
    "required":["pattern"]
}}
]

def run_bash(command:str):
    try :
        result=subprocess.run(args=command,shell=True,capture_output=True,timeout=120,errors="replace",text=True,cwd=WORKDIR)
        out=(result.stdout+result.stderr).strip()
        return (
            f"Exit code:{result.returncode}\n"
            f"output: \n{out[:500] if out else "no output"}")
        
    except subprocess.TimeoutExpired:
        return "Error : Timeout(120ms)"

def file_read(path : str,limit:int=200):
    
    try:
        lines=Path(WORKDIR/path).resolve().read_text(encoding="utf-8").splitlines()
        if limit and limit<len(lines):
         line=lines[:limit]+f"{len[lines]-limit} more lines"
        return '\n'.join(lines) 

    except Exception as e:
        return f"Error {e}"


def file_write(path:str ,content:str):
    try:
        file_path=Path(WORKDIR/path).resolve()
        file_path.parent.mkdir(parents=True,exist_ok=True)
        file_path.write_text(content,encoding="utf-8")
        return f"write {len(content)}bytes to the file "
    except Exception as e:
        return f"error{e}"


def file_edit(path:str,old_text:str,new_text:str):
    try:
        file_path=Path(WORKDIR/path).resolve()
        text=file_path.read_text(encoding='utf-8')
        file_path.write_text(text.replace(old_text,new_text,1),encoding='utf-8')
        return f"file edited"
    except Exception as e:
        return f"error:{e}"

def file_glob(pattern:str):
    import glob as g
    try:
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("(more matches..; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"





hooks={"UserPromptSubmit":[],"PreToolUse":[],"PostToolUse":[],"stop":[]}


# permission pipeline

#gate- 1 Hard deny list
blocked_commands=["rm -rf /","sudo","reboot","shutdown","mkfs", "dd if=", "> /dev/sda"]


#gate-2: context dependent checks

DESTRUCTIVE_COMMAND_WORD=re.compile(r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])")


def contains_destructive_command(command:str):
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))



def register_hook(event:str,callback):
    hooks[event].append(callback)

def trigger_hooks(event:str,*args):
    for callback in hooks[event]:
        result=callback(*args)
        if result is not None:
            return result
    return None


def context_inject_hook():
    print(f"\033[90m◆ Prompt received | Workspace: {Path.cwd()}\033[0m")
    return None

def permission_hook(block):
    "PreToolUse"
    if block.name=='bash':
        command= block.input.get("command" ," ")
        for pattern in blocked_commands:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                return "permission denied by denied list"
        if contains_destructive_command(command):
            print(f"\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"tool:{block.name}{block.input}")
            choice=input("Allow [y/n]").strip().lower()
            if choice not in ['y','yes']:
                return "permission denied by user"
        if block.name in ["read_file","write_file","edit_file"]:
            if not WORKDIR/block.input.get('path',"").resolve().is_relative_to(WORKDIR):
                print(f"\n\033[33m[permission] Access outside workspace\033[0m")
                print(f"tool:{block.name}{block.input}")
                choice=input("Allow [y/n]").strip().lower()
                if choice not in ['y','yes']:
                    return "permission denied by user"  
    return None  
    


def log_hook(block):
    "PreToolUse:log every tool  call"
    args=str(list(block.input.values())[:2])[:60]
    print(f"\033[90m→ {block.name}({args})\033[0m")
    handler=TOOL_HANDLERS.get(block.name)
    output = handler(**block.input)
    print(f"  ↳ {str(output)[:250]}")
    return None


def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def summary_hook(messages):
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m✓ Completed · {tool_count} tool calls\033[0m")
                 
    return

register_hook("UserPromptSubmit",context_inject_hook)
register_hook("PreToolUse",permission_hook)
register_hook("PreToolUse",log_hook)
register_hook("PostToolUse",large_output_hook)
register_hook("stop",summary_hook)






TOOL_HANDLERS={"bash":run_bash,"read_file":file_read,"write_file":file_write,"edit_file":file_edit,"global":file_glob}

def call_claude(messages):
    try:
        return client.messages.create(
            messages=messages,
            model=model,
            tools=Tools,
            max_tokens=5000,
            system=SYSTEM
        )

    except anthropic.APIConnectionError as e:
        print(f"\033[31m✗ Anthropic connection failed: {e}\033[0m")
        return None



def agent_loop(message:list):

    while True:
        tool_count=0
        max_tool=10
        if tool_count>=max_tool:
            return "Tool Execution limit reached"
        response=call_claude(message)
        if response is None:
            return "Anthropic api connection failed"

        message.append({'role':'assistant','content':response.content})

        tool_calls=[block for block in response.content if block.type=='tool_use']
        if not tool_calls:
            closed=trigger_hooks("stop",message)
            if closed:
                message.append({'role':'user','content':closed})
            return 
        
        result=[]
        for block in tool_calls:
            tool_count+=1
            blocked=trigger_hooks("PreToolUse",block)
            if blocked:
                result.append({"tool_use_id":block.id,"type":"tool_result","content":str(blocked)})
                continue    
            handler=TOOL_HANDLERS.get(block.name)
            try :
                output=handler(**block.input) if handler else f"Unknown:{block.name}"

                trigger_hooks('PostToolUse',block,output)
                result.append({"type":"tool_result","tool_use_id":block.id,"content":output})
            except anthropic.APIConnectionError as e:
                    print(f"\033[31m✗ Connection failed: {e}\033[0m")
                    print("Orbit is still running. Please try again.")

            except anthropic.APIError as e:
                print(f"\033[31m✗ Anthropic API error: {e}\033[0m")

        message.append({"role":"user",'content':result})


if __name__=='__main__':
    print("Enter question,press enter to send ,q to quit")
    history=[]
    while True:
        try:  
            query=input("\001\033[36m\002Orbit >> \001\033[0m\002")
        except (EOFError,KeyboardInterrupt):
            break

        if not query.strip():
            continue
        if query.strip().lower() in ['q','exit']:
            break
        trigger_hooks("UserPromptSubmit")
        history.append({"role":"user","content":query})
        output=agent_loop(history)
        for block in history[-1]['content']:
            if getattr(block,'type',None)=="text":
                print(block.text)

        print()         






