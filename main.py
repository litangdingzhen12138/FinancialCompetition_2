from text2sql import Text2SQLService
from text2sql.errors import Text2SQLError

service = Text2SQLService()
session_id = "interactive-console"

while(True):
    use_input = input("\n\033[1;31m请输入问题(按q,quit或exit退出)：\033[0m")
    if use_input.lower() in ['q','quit','exit']:
        break
    try:
        print(service.ask(use_input, session_id).answer)
    except (Text2SQLError, ValueError) as exc:
        print(f"处理失败：{exc}")

# print(service.ask("截至2026-03-31，各项存款余额排名前三的是哪几家？", "session-1").answer)
# print(service.ask("它们的不良率呢？", "session-1").answer)
