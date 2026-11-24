import csv
import io
import json
import csv
from datetime import datetime

import streamlit as st

from agent.core import DATA, Graph, ask, configuration, examples, save_examples, validate, schema, topology

st.set_page_config(page_title="代码图谱 Text2Cypher", layout="wide")
st.title("面向代码图谱的 Text2Cypher 查询智能体")
st.caption("教师 Neo4j 图谱 · 只读查询 · DeepSeek / GLM / Kimi")


def run_one(question, level="自动"):
    cfg = configuration()
    graph = Graph(cfg)
    try:
        return ask(question, graph, cfg, level=level)
    finally:
        graph.close()


def show(record, key="current"):
    if record.get("error"):
        st.error(record["error"])
    if record.get("answer_complete") is False:
        st.warning("结果达到受控上限，答案不完整；请缩小问题范围。")
    elif len(record.get("results", [])) > record.get("display_limit", 100):
        st.info(f"本次查询未触发取数上限，返回 {len(record['results'])} 行；表格仅展示前 {record.get('display_limit', 100)} 行，报告包含全部已取得结果。")
    st.write(record.get("summary", ""))
    for stage in record.get("stages", []):
        with st.expander(stage["stage"], expanded=stage["stage"] in ("错误", "校验")):
            st.json(stage)
    for index, query in enumerate(record.get("queries", []), 1):
        st.code(query, language="cypher")
    if record.get("results"):
        display_rows = [
            {field: json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else value
             for field, value in row.items()}
            for row in record["results"][:record.get("display_limit", 100)]
        ]
        st.dataframe(display_rows, use_container_width=True)
    topo = topology(record.get("results", []))
    if topo.get("nodes"):
        def esc(s): return str(s).replace('"', '\\"').replace("\n", " ")[:100]
        lines = ["digraph G {", "rankdir=LR;"]
        lines += [f'"{esc(n["id"])}" [label="{esc(n["label"])}"];' for n in topo["nodes"][:60]]
        lines += [f'"{esc(e["source"])}" -> "{esc(e["target"])}" [label="{esc(e.get("label", ""))}"];' for e in topo.get("edges", [])[:80]]
        st.graphviz_chart("\n".join(lines+["}"]), use_container_width=True)
    else:
        st.info("本次结果没有返回可绘制的图节点。可让模型返回 nodeId 和相关实体以显示拓扑。")
    safe_record = dict(record, topology=topo)
    report = "# 查询报告\n\n## 问题\n" + record["question"] + "\n\n## 总结\n" + record.get("summary", "") + "\n\n## Cypher\n" + "\n".join("```cypher\n"+q+"\n```" for q in record.get("queries", [])) + "\n\n## 过程与结果\n```json\n" + json.dumps(safe_record,ensure_ascii=False,indent=2,default=str) + "\n```\n"
    st.download_button("下载 Markdown 报告", report, file_name="text2cypher_report.md", mime="text/markdown", key="report_"+key)


tab_query, tab_batch, tab_history, tab_kb = st.tabs(["单条提问", "批量查询", "历史记录", "知识库管理"])
with tab_query:
    level = st.radio("难度入口", ["简单", "中等", "复杂"], horizontal=True)
    hints = {"简单":"例如：查找 ts-food-service 微服务", "中等":"例如：getTickets 调用了哪些下游服务", "复杂":"例如：ts-food-service 的 REST 与 MQ 联合依赖有哪些"}
    question = st.text_input("自然语言问题", placeholder=hints[level])
    if st.button("开始查询", type="primary", disabled=not question.strip()):
        with st.status("正在检索 Schema、生成、校验并执行查询", expanded=True) as status:
            result = run_one(question.strip(), level=level)
            status.update(label="查询结束" if not result["error"] else "查询失败", state="complete" if not result["error"] else "error")
        st.session_state["last_result"] = result
    if "last_result" in st.session_state:
        show(st.session_state["last_result"])
with tab_batch:
    uploaded = st.file_uploader("上传 TXT（每行一题）、CSV（question 列）或 JSON（字符串数组）", type=["txt","csv","json"])
    if uploaded and st.button("执行批量查询"):
        text = uploaded.getvalue().decode("utf-8-sig")
        if uploaded.name.endswith(".json"):
            questions = json.loads(text)
        elif uploaded.name.endswith(".csv"):
            questions = [r["question"] for r in csv.DictReader(io.StringIO(text))]
        else:
            questions = [line.strip() for line in text.splitlines() if line.strip()]
        questions = questions[:100]
        bar = st.progress(0)
        outputs = []
        for i, q in enumerate(questions):
            outputs.append(run_one(q))
            bar.progress((i+1)/len(questions), text=f"{i+1}/{len(questions)}")
        st.write(f"完成 {len(outputs)} 题，成功 {sum(not x['error'] for x in outputs)} 题")
        st.download_button("下载批量结果", json.dumps(outputs,ensure_ascii=False,indent=2,default=str),"batch_results.json")
with tab_history:
    history = DATA / "history.jsonl"
    if history.exists():
        lines = history.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(reversed(lines[-30:])):
            item = json.loads(line)
            with st.expander(item["question"]):
                show(item, key=f"history_{index}")
    else:
        st.info("暂无查询历史")
with tab_kb:
    items = examples()
    term = st.text_input("检索问题、分类或 Cypher")
    matches = [x for x in items if term.lower() in json.dumps(x,ensure_ascii=False).lower()]
    st.write(f"共 {len(items)} 组；当前显示 {len(matches)} 组")
    st.dataframe(matches, use_container_width=True)
    with st.form("kb_editor"):
        selected = st.selectbox("编辑已有条目，或选择新建", ["新建"]+[x["id"] for x in items])
        prior = next((x for x in items if x["id"] == selected), {})
        category = st.selectbox("分类", ["单节点","属性过滤","入口API","下游服务","反向影响","MQ链路","REST与MQ"], index=["单节点","属性过滤","入口API","下游服务","反向影响","MQ链路","REST与MQ"].index(prior.get("category","单节点")))
        q = st.text_input("问题", value=prior.get("question", ""))
        cy = st.text_area("只读 Cypher", value=prior.get("cypher", ""))
        save = st.form_submit_button("验证并保存")
        delete = st.form_submit_button("删除所选条目")
    if save:
        try:
            safe = validate(cy, schema())
            cfg = configuration(); graph = Graph(cfg)
            try: rows = graph.run(safe)
            finally: graph.close()
            if not rows: raise ValueError("查询结果为空，不保存")
            item = {"id": selected if selected != "新建" else "K"+datetime.now().strftime("%Y%m%d%H%M%S"),"category":category,"question":q.strip(),"cypher":cy.strip()}
            if not item["question"]: raise ValueError("问题不能为空")
            save_examples([x for x in items if x["id"] != item["id"]]+[item]); st.success(f"已验证并保存，结果 {len(rows)} 行")
        except Exception as exc: st.error(str(exc))
    if delete and selected != "新建":
        save_examples([x for x in items if x["id"] != selected]); st.success("已删除")
