"""Create local ignored config from the assignment PDF without echoing secrets."""
import os
import re
from pathlib import Path
import pymupdf

root=Path(__file__).resolve().parents[1]
dest=root/".env"
if dest.exists(): raise SystemExit(".env 已存在；未覆盖")
pdf=pymupdf.open(os.environ["ASSIGNMENT_PDF"])
password=re.search(r"密码[:：]\s*([^\s]+)",pdf[13].get_text()).group(1)
template=(root/".env.example").read_text(encoding="utf-8")
dest.write_text(template.replace("NEO4J_PASSWORD=", "NEO4J_PASSWORD="+password,1),encoding="utf-8")
print("已创建本地 .env；请自行填写允许的模型密钥")
