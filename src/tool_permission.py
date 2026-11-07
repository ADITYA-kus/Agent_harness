import os 
import subprocess
import json
from pathlib import Path
import re 
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
client=Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model=os.getenv("MODEL_ID")

WORKDIR=Path.cwd
ENVIRONMENT_PROMPT=(
    "Windose:use bash toll cmd.exe ,not unix"
    if os.name=='nt'
    else "unix like : bash tool turn on using unix bash"

)


SYSTEM=(
    f"you are coding agent working on {WORKDIR},Environment {ENVIRONMENT_PROMPT}"
    "use tools to solve task ,do not use permission yourself"
    "the harness will approve or deny before execution"
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
        result=subprocess.run(args=command,shell=True,capture_output=True,timeout=120,errors=replace,text=True,cmd=WORKDIR)
        out=(result.stdout+result.stderr).strip()
        return out[:5000] if out else "no output"
    except subprocess.TimeoutExpired:
        return "Error : Timeout(120ms)"

def file_read(path : str,limit:int):
    
    try:
        lines=Path(WORKDIR/path).resolve().read_text(encoding="utf-8").splitlines()
        if limit and limit<len(lines):
         line=lines[:limit]+f"{len[lines]-limit} more lines"
        return '\n'.join(line) 

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

def lsp_definition(file:str,line:int,character:int):
    try:
        file_path=Path(WORKDIR/file).resolve()
        if not file_path.is_relative_to(WORKDIR):
            return f"error : file outside of directory"
        if line <0 and character<0:
            return f"error: invalid position"

    except Exception as e:
        pass        





# permission pipeline

#gate- 1 Hard deny list
blocked_commands=["rm -rf /","sudo","reboot","shutdown","mkfs", "dd if=", "> /dev/sda"]


def check_block_command(command:str):
    for pattern in blocked_commands:
        if pattern in command:
            return f"Blocked:{pattern} is in the deny lsit"
    return None

#gate-2: context dependent checks

DESTRUCTIVE_COMMAND_WORD=re.compile(r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])")


def contains_destructive_command(command:str):
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


#gate-3 user approval

def ask_user(tool_name:str,args:dict,reason:str):
     print(f"\n\033[33m[permission] {reason}\033[0m")
     print(f"tool:{tool_name}args:{args}")
     choice=input("Allow[Y/N]").strip().lower()
     return "Allow" if choice in ['y','yes'] else 'deny'

Permission_rule=[

    {"tools":['read_file','write_file','edit_file'],
     "check":lambda args:not Path(WORKDIR/args.get("path","")).resolve().is_relative_to(WORKDIR),
     "message":'writing outside of workspace'
    },
{"tools":"bash",
"check":lambda args: contains_destructive_command(args.get("command"," ")),
"message":"potentially destructive command"}]

def check_rules(tool_name:str,args:dict):
    for rule in Permission_rule:
        if tool_name in rule and rule[check](args):
            return rule[message]
    return None   
 


def check_permission(block):

    if block.name=='bash':
        reason=check_block_command(block.input.get("command"," "))
        if reason:
            return False
    reason=check_rules(block.name,block.input)
    if reason:
        decesion=ask_user(block.name,block.input,reason) 
        if decesion=='deny':
            return False
    return True


TOOL_HANDLERS={"bash":run_bash,"read_file":file_read,"write_file":file_write,"edit_file":file_edit,"global":file_glob}


def agent_loop(message:list):
    while True:
        response=client.messages.create(messages=message,model=model,tools=Tools,max_tokens=5000)
        message.append({'role':'assistant','content':response.content})

        tool_calls=[block for block in response.content if block.type=='tool_use']
        if not tool_calls:
            return 
        result=[]

        for block in tool_calls:
            if not check_permission(block):
                result.append({"tool_use_id":block.id,"type":"tool_result","content":"permission denied"})
            continue    
        handler=TOOL_HANDLERS.get(block.name)
        output=handler(**block.input) if handler else f"Unknown:{block.name}"
        print(str(output[:100]))
        result.append({"type":"tool_result","tool_use_id":block.id,"content":output})

    message.append({"role":"user",'content':result})


if __name__=='__main__':
    print("Enter question,press enter to send ,q to quit")
    history=[]
    while True:
        try:  
            query=input("\001\033[36m\002s03 >> \001\033[0m\002")
        except (EOFError,KeyboardInterrupt):
            break

        if not query.strip():
            continue
        if query.strip().lower() in ['q','exit']:
            break
        history.append({"role":"user","content":query})
        output=agent_loop(history)
        for block in history[-1]['content']:
            if getattr(block,'type',None)=="text":
                print(block.text)

        print()         










            








    


        



              

    













    





















