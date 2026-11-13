import json
import os
import re
import time
import urllib.request
import urllib.error
import socket
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS, Query
from neo4j.graph import Node, Relationship, Path as GraphPath

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def schema():
    return json.loads((DATA / "schema.json").read_text(encoding="utf-8"))


def configuration():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    if provider not in {"deepseek", "glm", "kimi"}:
        raise ValueError("仅允许 DeepSeek、GLM 或 Kimi")
    return {"uri": os.getenv("NEO4J_URI", ""), "user": os.getenv("NEO4J_USER", ""),
            "password": os.getenv("NEO4J_PASSWORD", ""), "provider": provider,
            "key": os.getenv("LLM_API_KEY", ""), "model": os.getenv("LLM_MODEL", ""),
            "timeout": min(30, max(1, int(os.getenv("QUERY_TIMEOUT_SECONDS", "8"))))}


class Graph:
    def __init__(self, cfg):
        if not all(cfg.get(k) for k in ("uri", "user", "password")):
            raise RuntimeError("请在 .env 中配置 Neo4j 连接")
        self.driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]), connection_timeout=8)
        self.timeout = cfg["timeout"]

    def close(self):
        self.driver.close()

    def run(self, cypher, params=None):
        with self.driver.session(default_access_mode=READ_ACCESS, fetch_size=100) as s:
            s.run(Query("EXPLAIN " + cypher, timeout=self.timeout), params or {}).consume()
            return [{key: graph_value(row[key]) for key in row.keys()}
                    for row in s.run(Query(cypher, timeout=self.timeout), params or {})]


def graph_value(value):
    """Keep relationship direction supplied by Neo4j instead of guessing from row order."""
    if isinstance(value, Node):
        return dict(value)
    if isinstance(value, Relationship):
        return {"__graph_edge__": True, "type": value.type,
                "source": value.start_node.get("nodeId"), "target": value.end_node.get("nodeId"),
                "source_node": dict(value.start_node), "target_node": dict(value.end_node),
                "properties": dict(value)}
    if isinstance(value, GraphPath):
        return {"__graph_path__": True, "nodes": [graph_value(n) for n in value.nodes],
                "relationships": [graph_value(r) for r in value.relationships]}
    if isinstance(value, list):
        return [graph_value(item) for item in value]
    if isinstance(value, dict):
        return {key: graph_value(item) for key, item in value.items()}
    return value


FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL|SHOW|USE|GRANT|DENY|REVOKE|ALTER|START|STOP|APOC|DBMS)\b", re.I)
LABEL = re.compile(r":`?([\w\u4e00-\u9fff]+)`?")
PROPERTY = re.compile(r"\b\w+\.(`[^`]+`|[\w\u4e00-\u9fff]+)")


def mask_cypher_quotes(query, identifiers=False):
    """Keep offsets while hiding literals (and optionally quoted identifiers)."""
    hidden = list(query)
    index = 0
    while index < len(query):
        quote = query[index]
        if quote not in ("'", '"') and not (identifiers and quote == chr(96)):
            index += 1
            continue
        start = index
        index += 1
        while index < len(query):
            if query[index] == "\\" and quote != chr(96):
                index += 2
                continue
            if query[index] == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    index += 2
                    continue
                index += 1
                break
            index += 1
        else:
            raise ValueError("Cypher 字符串或标识符未闭合")
        hidden[start:index] = " " * (index - start)
    return "".join(hidden)


def validate(query, info):
    q = query.strip()
    if not q or len(q) > 8000:
        raise ValueError("查询为空或过长")
    outside = mask_cypher_quotes(q)
    if outside.rstrip().endswith(";"):
        q = q[:-1].rstrip()
        outside = outside[:-1].rstrip()
    if ";" in outside or "//" in outside or "/*" in outside or "*/" in outside:
        raise ValueError("查询为空、过长或含注释/多语句")
    keywords = mask_cypher_quotes(q, identifiers=True)
    if FORBIDDEN.search(keywords) or not re.match(r"^(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND)\b", keywords, re.I):
        raise ValueError("仅支持只读 MATCH/WITH/UNWIND 查询")
    if not re.search(r"\bRETURN\b", keywords, re.I):
        raise ValueError("查询必须返回结果")
    allowed = set(info["labels"]) | set(info["relationships"])
    without_literals = mask_cypher_quotes(q)
    for token in LABEL.findall(without_literals):
        if token not in allowed:
            raise ValueError("Schema 中不存在标签或关系：" + token)
    props = {p for values in info["properties"].values() for p in values}
    props.update(p for relation in info.get("relationship_properties", []) for p in relation.get("properties", []))
    for token in PROPERTY.findall(without_literals):
        prop = token.strip("`")
        if prop not in props:
            raise ValueError("Schema 中不存在属性：" + prop)
    for match in re.finditer(r"\bLIMIT\s+(\d+)\b", without_literals, re.I):
        if int(match.group(1)) > 100:
            raise ValueError("单次查询最多返回 100 行")
    for relation in re.findall(r"\[[^\]]*\]", without_literals):
        match = re.search(r"\*(\d*)(?:\.\.(\d*))?", relation)
        if match:
            low, high = match.groups()
            if not low or (high is not None and not high) or int(high if high is not None else low) > 5:
                raise ValueError("路径遍历必须限定在 1–5 跳")
    if not re.search(r"\bLIMIT\s+\d+\b", keywords, re.I):
        q += " LIMIT 100"
    return q


def examples():
    path = DATA / "knowledge.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_examples(items):
    (DATA / "knowledge.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def lexical_score(question, item):
    terms = set(re.findall(r"[A-Za-z][\w-]*|[\u4e00-\u9fff]{2,}", question.lower()))
    body = (item["question"] + " " + item.get("category", "")).lower()
    return sum(2 if t in body else 0 for t in terms) + sum(1 for t in re.findall(r"[\u4e00-\u9fff]{2}", question) if t in body)


def intent_profile(text):
    """Map paraphrases to graph-query intents without any external model."""
    value = text.lower()
    tags = set()
    has_mq = bool(re.search(r"消息|队列|交换机|mq|rabbit|异步|发布|消费|接收|监听", value))
    if has_mq and re.search(r"完整|全链路|全程|从.*到|发布.*消费|生产.*消费", value):
        tags.add("mq_full_chain")
    if has_mq and re.search(r"消费|接收|监听|订阅|处理消息", value):
        tags.add("mq_consumer")
    if has_mq and re.search(r"发布|发送|生产|发出|推送", value):
        tags.add("mq_publisher")
    if has_mq and re.search(r"路由|交换机|exchange", value):
        tags.add("mq_route")
    if re.search(r"同步.*异步|异步.*同步|rest.*mq|mq.*rest|rpc.*mq|mq.*rpc|联合依赖|同时.*(消息|mq)", value):
        tags.add("rest_mq_union")
    if re.search(r"修改|变更|移除|删除|影响|波及|受牵连|反向", value):
        tags.add("impact")
    if re.search(r"映射|外部调用关系|对应.*入口", value):
        tags.add("api_mapping")
    if re.search(r"入口|暴露|endpoint|对外接口|路由地址", value):
        tags.add("entry_api")
    if re.search(r"下游|目标服务|调用.*服务|依赖.*服务", value):
        tags.add("downstream_service")
    if re.search(r"方法.*调用|调用.*方法|调用链|一到[两三四五]跳", value):
        tags.add("method_call")
    if re.search(r"有多少|数量|总数|计数", value):
        tags.add("count")
    return tags


def retrieve_ranked(question, limit=4):
    query_intents = intent_profile(question)
    entity_pattern = r"ts-[a-z0-9-]+|[a-z]+_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9_]*"
    entities = {term.lower() for term in re.findall(entity_pattern, question)}
    ranked = []
    for item in examples():
        item_intents = intent_profile(item["question"])
        if item["category"] == "REST与MQ":
            item_intents.add("rest_mq_union")
        elif item["category"] == "反向影响" and "impact" in item_intents:
            item_intents.add("impact")
        overlap = sorted(query_intents & item_intents)
        same_entities = sorted(entity for entity in entities if entity in item["question"].lower())
        item_entities = {term.lower() for term in re.findall(entity_pattern, item["question"])}
        extra_entities = sorted(item_entities - entities)
        lexical = lexical_score(question, item)
        score = 12 * len(overlap) + 5 * len(same_entities) - 2 * len(extra_entities) + min(lexical, 10) * 0.4
        if query_intents and item_intents and not overlap:
            score -= 4
        ranked.append({"example": item, "score": round(score, 2),
                       "reasons": {"shared_intents": overlap, "shared_entities": same_entities,
                                   "extra_entities": extra_entities, "lexical_score": lexical}})
    return sorted(ranked, key=lambda row: (-row["score"], row["example"]["id"]))[:limit]


def retrieve(question, limit=4):
    return [row["example"] for row in retrieve_ranked(question, limit)]


def recognize(question, graph, info):
    terms = list(dict.fromkeys(re.findall(r"ts-[a-z0-9-]+|/[A-Za-z0-9_/{}/-]+|[A-Za-z_][\w.-]{2,}", question)))[:16]
    found = []
    if not terms:
        return found
    for label, prop in (("微服务","服务名称"),("方法","方法名"),("类","简单名"),
                        ("上游API","API路径"),("下游API","API路径"),("消息队列","队列名称")):
        if prop not in info["properties"].get(label, []):
            continue
        full = "n.全限定名" if "全限定名" in info["properties"].get(label, []) else "null"
        query = f"MATCH (n:`{label}`) WHERE n.`{prop}` IN $terms RETURN n.nodeId AS id, n.`{prop}` AS name, {full} AS full_name LIMIT 30"
        for row in graph.run(query,{"terms":terms}):
            found.append({"label":label,"property":prop,"value":row["name"],"nodeId":row["id"],"full_name":row["full_name"]})
    return found


PROVIDERS = {"deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash"),
             "glm": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash"),
             "kimi": ("https://api.moonshot.cn/v1/chat/completions", "moonshot-v1-8k")}


def generate(messages, cfg):
    if not cfg["key"]:
        raise RuntimeError("未配置允许的模型密钥 LLM_API_KEY")
    endpoint, default_model = PROVIDERS[cfg["provider"]]
    body = json.dumps({"model": cfg["model"] or default_model, "temperature": 0,
                       "messages": messages}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"Authorization": "Bearer " + cfg["key"],
                         "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)["choices"][0]["message"]["content"]
        except (TimeoutError, socket.timeout, urllib.error.URLError, urllib.error.HTTPError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 2:
                raise
            time.sleep(attempt + 1)


def parse_queries(text):
    cleaned = re.sub(r"```(?:json|cypher)?|```", "", text, flags=re.I).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            data = data.get("queries") or [data.get("cypher", "")]
        return [x["cypher"] if isinstance(x, dict) else x for x in data]
    except (ValueError, TypeError, KeyError):
        return [cleaned]


def topology(rows):
    nodes, edges = {}, {}
    def visit(value):
        if isinstance(value, dict):
            if "nodeId" in value:
                nodes[value["nodeId"]] = {"id": value["nodeId"], "label": next((str(v) for k,v in value.items() if k in ("服务名称", "方法名", "简单名", "API路径", "队列名称")), value["nodeId"])}
            if value.get("__graph_edge__") and value.get("source") and value.get("target"):
                key = (value["source"], value["target"], value.get("type", ""))
                edges[key] = {"source": key[0], "target": key[1], "label": key[2]}
            for v in value.values(): visit(v)
        elif isinstance(value, list):
            for v in value: visit(v)
    visit(rows)
    return {"nodes": list(nodes.values()), "edges": [edge for edge in edges.values()
            if edge["source"] in nodes and edge["target"] in nodes]}


def result_summary(rows, query_count):
    if not rows:
        return f"已执行 {query_count} 个只读查询，没有找到符合条件的记录。"
    previews = []
    for row in rows[:3]:
        fields = [f"{key}为{str(value)[:90]}" for key, value in row.items() if value is not None]
        if fields:
            previews.append("、".join(fields[:3]))
    detail = "；前几条结果是：" + "；".join(previews) if previews else "。"
    return f"已执行 {query_count} 个只读查询，合并去重后找到 {len(rows)} 条记录" + detail


MAX_ANSWER_ROWS = 500
DISPLAY_ROWS = 100
OPEN_ENDED = re.compile(r"哪些|所有|全部|完整|全链路|列出|有谁|有哪些|分别是谁|如何组合|多少种")
EXPLICIT_SIZE = re.compile(r"前[一二三四五六七八九十百\d]+|[一二三四五六七八九十百\d]+(?:个|条|项)")
TAIL_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.I)


def bounded_query(query, question):
    """Fetch one witness row beyond the answer cap, keeping generated Cypher read-only."""
    match = TAIL_LIMIT.search(query)
    if not match:
        raise ValueError("受控查询缺少末尾 LIMIT")
    requested = int(match.group(1))
    cap = MAX_ANSWER_ROWS if OPEN_ENDED.search(question) and not EXPLICIT_SIZE.search(question) else requested
    return query[:match.start()] + f"LIMIT {cap + 1}", cap


def clamp_generated_limit(query):
    """Repair a single oversized final LIMIT before strict validation."""
    match = TAIL_LIMIT.search(query)
    if match and int(match.group(1)) > 100:
        return query[:match.start()] + "LIMIT 100", True
    return query, False


def clamp_generated_paths(query):
    """Bound only generated relationship quantifiers, preserving a zero-hop lower bound."""
    changed = False
    def relation(match):
        nonlocal changed
        body = match.group(0)
        if ":" not in body or "*" not in body:
            return body
        def bound(length):
            nonlocal changed
            low, high = length.groups()
            if low and int(low) > 5:
                return length.group(0)
            if not low or high is None or high == "" or int(high) > 5:
                changed = True
                return "*" + (low or "1") + "..5"
            return length.group(0)
        return re.sub(r"\*(\d*)(?:\.\.(\d*))?", bound, body)
    fixed = re.sub(r"\[[^\]]*\]", relation, query)
    return fixed, changed


def full_chain_intent(question):
    return ("消息" in question or "MQ" in question.upper()) and (
        "全链路" in question or "完整链路" in question or
        all(word in question for word in ("发布", "消费", "链路")))


def chain_candidate(row, index):
    names = {"publisher": ("发布", "publisher"), "exchange": ("交换机", "exchange"),
             "queue": ("队列", "queue"), "consumer": ("消费", "consumer")}
    candidate = {"i": index}
    for role, aliases in names.items():
        candidate[role] = next((value["nodeId"] for key, value in row.items()
            if any(alias in key.lower() for alias in aliases) and isinstance(value, dict)
            and value.get("nodeId")), None)
    return candidate if all(candidate.get(role) for role in names) else None


def chain_scalar_candidate(row, index, entities=()):
    """Bind scalar endpoints to the queue and service named in the question."""
    def value_for(*names):
        options = [(key, value) for key, value in row.items() if isinstance(value, str)
                   and value and any(name in key for name in names)
                   and not any(suffix in key for suffix in ("ID", "nodeId"))]
        options.sort(key=lambda pair: ("全限定名" not in pair[0], "名称" not in pair[0], len(pair[0])))
        return options[0][1] if options else None
    queue_names = {item["value"] for item in entities if item.get("label") == "消息队列"}
    service_names = {item["value"] for item in entities if item.get("label") == "微服务"}
    row_queue = value_for("队列名称") or next(
        (value for key, value in row.items() if key in ("队列", "消息队列")
         and isinstance(value, str) and value), None)
    question_queue = next(iter(queue_names)) if len(queue_names) == 1 else None
    queue = row_queue or question_queue
    row_service = value_for("队列所属服务", "队列服务")
    question_service = next(iter(service_names)) if len(service_names) == 1 else None
    queue_service = row_service or question_service
    route_keys = {value for key, value in row.items() if isinstance(value, str)
                  and value and "路由键" in key}
    result = {"i": index, "publisher": value_for("发布者", "发布方法"),
              "exchange": value_for("交换机"), "queue": queue,
              "queue_service": queue_service, "consumer": value_for("消费者", "消费方法"),
              "routing_key": next(iter(route_keys)) if len(route_keys) == 1 else None,
              "conflicting_constraints": len(route_keys) > 1 or bool(
                  row_queue and question_queue and row_queue != question_queue) or bool(
                  row_service and question_service and row_service != question_service)}
    return result if result["publisher"] and result["consumer"] and result["queue"] else None


def witnessed_chain(row):
    evidence = []
    def collect(value):
        if isinstance(value, dict):
            if value.get("__graph_edge__"):
                evidence.append(value)
            for nested in value.values(): collect(nested)
        elif isinstance(value, list):
            for nested in value: collect(nested)
    collect(row)
    return any(p.get("type") == "发布至" and e.get("type") == "路由至" and
               c.get("type") == "消费自" and p.get("target") == e.get("source") and
               e.get("target") == c.get("source") and
               p.get("properties", {}).get("路由键") is not None and
               p.get("properties", {}).get("路由键") == e.get("properties", {}).get("路由键")
               for p in evidence for e in evidence for c in evidence)


def route_delivery_intent(question):
    return bool(re.search(r"消息|队列|MQ|异步", question, re.I)) and bool(
        re.search(r"目标|下游|发布|发出|发送|接收|消费|链路|依赖|通信", question))


def route_evidence(row):
    edges = []
    def collect(value):
        if isinstance(value, dict):
            if value.get("__graph_edge__"):
                edges.append(value)
            for nested in value.values(): collect(nested)
        elif isinstance(value, list):
            for nested in value: collect(nested)
    collect(row)
    pairs = [(pub, route) for pub in edges if pub.get("type") == "发布至"
             for route in edges if route.get("type") == "路由至"
             and pub.get("target") == route.get("source")]
    if not pairs:
        return None
    return any(pub.get("properties", {}).get("路由键") is not None and
               pub.get("properties", {}).get("路由键") == route.get("properties", {}).get("路由键")
               for pub, route in pairs)


def route_candidate(row, index, node_ids):
    aliases = {"publisher": ("发布", "publisher"), "exchange": ("交换机", "exchange"),
               "queue": ("队列", "queue")}
    candidate = {"i": index}
    for role, names in aliases.items():
        options = []
        for key, value in row.items():
            if not any(name in key.lower() for name in names) or "服务" in key:
                continue
            if isinstance(value, dict) and value.get("nodeId"):
                options.append(value["nodeId"])
            elif isinstance(value, str):
                if node_ids and value.startswith(("mtd:", "exchange:", "queue:")):
                    options.append(value)
                elif not node_ids and not value.startswith(("mtd:", "exchange:", "queue:")):
                    options.append(value)
        candidate[role] = options[0] if options else None
    return candidate if candidate.get("publisher") and candidate.get("queue") else None


def filter_routed_messages(question, rows, graph):
    """Remove only rows claiming publisher-to-queue delivery without matching routing keys."""
    if not route_delivery_intent(question):
        return rows, {"stage": "路由语义", "required": False}
    valid, invalid = set(), set()
    id_candidates, scalar_candidates = [], []
    for index, row in enumerate(rows):
        evidence = route_evidence(row)
        if evidence is True:
            valid.add(index)
        elif evidence is False:
            invalid.add(index)
        else:
            by_id = route_candidate(row, index, True)
            by_name = route_candidate(row, index, False)
            if by_id:
                id_candidates.append(by_id)
            elif by_name:
                scalar_candidates.append(by_name)
            else:
                valid.add(index)  # REST rows, service-level MQ dependencies, or consumer-only rows.
    id_query = ("UNWIND $candidates AS candidate "
                "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) "
                "WHERE p.nodeId=candidate.publisher AND q.nodeId=candidate.queue "
                "AND (candidate.exchange IS NULL OR e.nodeId=candidate.exchange) "
                "AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
                "RETURN DISTINCT candidate.i AS i LIMIT 501")
    scalar_query = ("UNWIND $candidates AS candidate "
                    "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) "
                    "WHERE p.全限定名=candidate.publisher AND q.队列名称=candidate.queue "
                    "AND (candidate.exchange IS NULL OR e.交换机名称=candidate.exchange) "
                    "AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
                    "RETURN DISTINCT candidate.i AS i LIMIT 501")
    if id_candidates:
        valid.update(row["i"] for row in graph.run(id_query, {"candidates": id_candidates}))
    if scalar_candidates:
        valid.update(row["i"] for row in graph.run(scalar_query, {"candidates": scalar_candidates}))
    filtered = [row for index, row in enumerate(rows) if index in valid]
    return filtered, {"stage": "路由语义", "required": True, "verified_rows": len(filtered),
                      "discarded": len(rows)-len(filtered),
                      "reason": "发布路由键与队列绑定键必须一致，避免共享交换机产生伪送达路径"}


def verify_complete_chains(question, rows, graph, entities=()):
    """Only claim a full MQ chain when every edge is witnessed in the graph."""
    if not full_chain_intent(question):
        return rows, {"stage": "链路语义", "required": False}
    witnessed = {i for i, row in enumerate(rows) if witnessed_chain(row)}
    candidates, scalar_candidates, insufficient = [], [], []
    rejected_constraints = 0
    for index, row in enumerate(rows):
        if index in witnessed:
            continue
        candidate = chain_candidate(row, index)
        if candidate:
            candidates.append(candidate)
            continue
        candidate = chain_scalar_candidate(row, index, entities)
        if candidate:
            if candidate.pop("conflicting_constraints"):
                rejected_constraints += 1
            else:
                scalar_candidates.append(candidate)
        else:
            insufficient.append(index)
    if not candidates and not scalar_candidates and not witnessed:
        return [], {"stage": "链路语义", "required": True, "verified": 0,
                    "discarded": len(rows), "insufficient_evidence": len(insufficient),
                    "insufficient_indices": insufficient,
                    "rejected": rejected_constraints,
                    "reason": "结果缺少可验证身份或与题目队列、服务、路由键条件冲突；不能确认完整链路"}
    check = ("UNWIND $candidates AS candidate "
             "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) "
             "WHERE p.nodeId=candidate.publisher AND e.nodeId=candidate.exchange "
             "AND q.nodeId=candidate.queue AND c.nodeId=candidate.consumer "
             "AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
             "RETURN DISTINCT candidate.i AS i LIMIT 501")
    scalar_check = ("UNWIND $candidates AS candidate "
                    "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) "
                    "WHERE p.全限定名=candidate.publisher AND c.全限定名=candidate.consumer "
                    "AND q.队列名称=candidate.queue "
                    "AND (candidate.exchange IS NULL OR e.交换机名称=candidate.exchange) "
                    "AND (candidate.queue_service IS NULL OR q.所属服务名=candidate.queue_service) "
                    "AND (candidate.routing_key IS NULL OR pub.路由键=candidate.routing_key) "
                    "AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 "
                    "RETURN DISTINCT candidate.i AS i LIMIT 501")
    verified = witnessed | ({row["i"] for row in graph.run(check, {"candidates": candidates})}
                            if candidates else set())
    if scalar_candidates:
        verified |= {row["i"] for row in graph.run(scalar_check, {"candidates": scalar_candidates})}
    filtered = [row for i, row in enumerate(rows) if i in verified]
    return filtered, {"stage": "链路语义", "required": True, "verified": len(filtered),
                      "discarded": len(rows)-len(filtered),
                      "insufficient_evidence": len(insufficient),
                      "insufficient_indices": insufficient,
                      "rejected": len(rows)-len(filtered)-len(insufficient),
                      "reason": "只保留发布→路由→消费三段真实关系均存在的闭合链路"}


def ask(question, graph, cfg, model=generate, level="自动"):
    started = time.time()
    record = {"question": question, "level": level, "stages": [], "queries": [], "results": [], "topology": {}, "summary": "", "error": None,
              "answer_complete": True, "display_limit": DISPLAY_ROWS, "truncated_subqueries": []}
    try:
        info = schema()
        ranking = retrieve_ranked(question)
        retrieved = [row["example"] for row in ranking]
        entities = recognize(question, graph, info)
        record["stages"].append({"stage": "检索", "examples": [x.get("id") for x in retrieved],
                                  "ranking": [{"id": x["example"]["id"], "score": x["score"],
                                               "reasons": x["reasons"]} for x in ranking], "entities": entities})
        compact = {"labels": info["labels"], "properties": info["properties"],
                   "relationship_properties": info.get("relationship_properties", []),
                   "patterns": info["patterns"]}
        system = "你是只读 Neo4j Cypher 生成器。只使用提供的真实 Schema。只输出 JSON：{\"queries\":[\"MATCH ... RETURN ... LIMIT 100\"]}。难度为简单时优先单节点或单跳；中等时支持多跳和聚合；复杂时分析是否需要把变更影响或 RPC+MQ 联合问题拆成多个独立子查询。开放式列举使用 DISTINCT 去重；不要把 LIMIT 当成答案范围。凡用 发布至→路由至 推断消息实际到达队列，必须比较两条关系的 路由键，相等且非空才算真实路由；默认交换机的共享节点不足以证明消息送达。用户问 MQ 完整发布到消费链路时，必须让发布者→交换机→队列→消费者四类节点及三段关系组成闭合路径，返回这些节点或真实 path；部分链路问题可以使用 OPTIONAL MATCH，但不得把无消费者的部分链路当完整链路。优先返回相关节点和真实关系或 path 以绘制拓扑，也返回用户问题要求的字段。遇到同名实体用全限定名或 nodeId 消歧；不要编造标签属性，不要写入。"
        user = json.dumps({"question":question,"level":level,"schema":compact,"entities":entities,
                           "examples":[{"question":x["question"],"cypher":x["cypher"]} for x in retrieved]},ensure_ascii=False)
        raw = model([{"role":"system","content":system},{"role":"user","content":user}],cfg)
        record["stages"].append({"stage":"生成","raw":raw})
        queries = parse_queries(raw)
        if not 1 <= len(queries) <= 4:
            raise ValueError("子查询数量必须为 1–4")
        record["stages"].append({"stage":"问题拆分","subquery_count":len(queries)})
        all_rows = []
        for index, original in enumerate(queries, 1):
            repair_applied = False
            try:
                candidate, repaired_limit = clamp_generated_limit(original)
                candidate, repaired_path = clamp_generated_paths(candidate)
                if repaired_limit or repaired_path:
                    record["stages"].append({"stage":"校验修正","subquery":index,
                                              "reason":"模型末尾 LIMIT 或路径上限超过受控范围，已收紧后重新校验",
                                              "original":original,"cypher":candidate})
                q = validate(candidate, info)
            except Exception as exc:
                record["stages"].append({"stage":"校验","subquery":index,"cypher":original,"passed":False,"error":str(exc)})
                if model is not generate or not cfg.get("key"):
                    raise
                repair_messages = [
                    {"role":"system","content":"你是 Cypher 查询修正器。只输出 JSON：{\"queries\":[\"MATCH ... RETURN ... LIMIT 100\"]}，且恰好一条查询。不得使用 CALL、过程、写操作或不存在于 Schema 的字段。保留原子查询意图和结果投影。"},
                    {"role":"user","content":json.dumps({"question":question,"schema":compact,
                        "failed_cypher":original,"validation_error":str(exc)},ensure_ascii=False)}]
                repaired_raw = model(repair_messages,cfg)
                record["stages"].append({"stage":"生成修正","subquery":index,"raw":repaired_raw,
                                          "reason":str(exc)})
                replacements = parse_queries(repaired_raw)
                if len(replacements) != 1:
                    raise ValueError("修正后必须恰好返回一条子查询")
                replacement = replacements[0]
                candidate, repaired_limit = clamp_generated_limit(replacement)
                candidate, repaired_path = clamp_generated_paths(candidate)
                if repaired_limit or repaired_path:
                    record["stages"].append({"stage":"校验修正","subquery":index,
                                              "reason":"修正查询的 LIMIT 或路径上限超过受控范围",
                                              "original":replacement,"cypher":candidate})
                q = validate(candidate, info)
                repair_applied = True
            record["queries"].append(q)
            record["stages"].append({"stage":"校验","subquery":index,"cypher":q,
                                      "passed":True,"after_repair":repair_applied})
            try:
                execution_query, cap = bounded_query(q, question)
                fetched = graph.run(execution_query)
                truncated = len(fetched) > cap
                rows = fetched[:cap]
                if truncated:
                    record["answer_complete"] = False
                    record["truncated_subqueries"].append(index)
                record["stages"].append({"stage":"执行","subquery":index,"count":len(rows),
                    "fetched":len(fetched),"answer_cap":cap,"truncated":truncated,
                    "execution_cypher":execution_query,"passed":True})
            except Exception as exc:
                record["stages"].append({"stage":"执行","subquery":index,"passed":False,"error":str(exc)})
                raise
            all_rows.extend(rows)
        seen = set()
        for row in all_rows:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key); record["results"].append(row)
        before_route = len(record["results"])
        record["results"], route_stage = filter_routed_messages(question, record["results"], graph)
        record["stages"].append(route_stage)
        record["discarded_mismatched_route_rows"] = before_route-len(record["results"])
        if full_chain_intent(question):
            chain_input = list(record["results"])
            original_count = len(record["results"])
            record["results"], chain_stage = verify_complete_chains(question, record["results"], graph, entities)
            record["stages"].append(chain_stage)
            record["discarded_partial_chain_rows"] = original_count-len(record["results"])
            if chain_stage.get("insufficient_evidence"):
                record["unverified_chain_rows"] = [chain_input[i] for i in chain_stage["insufficient_indices"]]
            if (not record["results"] and original_count) or chain_stage.get("insufficient_evidence"):
                record["answer_complete"] = False
        record["topology"] = topology(record["results"])
        record["summary"] = result_summary(record["results"], len(queries))
        if record["truncated_subqueries"]:
            record["summary"] = f"结果超过受控上限 {MAX_ANSWER_ROWS} 行，当前答案不完整；已保留前 {len(record['results'])} 条，不能据此断言全集。"
        elif not record["answer_complete"]:
            record["summary"] = (f"完整消息链路证据不足或未通过图谱复核；仅保留 {len(record['results'])} 条已验证结果，"
                                 "被排除的原始行与原因保留在阶段记录中，不能据此断言答案完整。")
        elif len(record["results"]) > 20:
            record["summary"] = f"本次 Cypher 未触发取数上限，返回 {len(record['results'])} 条；界面只展示前 {DISPLAY_ROWS} 条，已取得的全部结果可在报告中查看。是否覆盖问题所需实体仍须核对查询语义。"
        if cfg.get("key") and model is generate and record["results"] and record["answer_complete"] and len(record["results"]) <= 20:
            try:
                messages = [{"role":"system","content":"仅根据给定的查询结果，用一两句中文回答原问题。不得补充数据中没有的事实；如果结果不完整，说明只展示了部分记录。"},
                            {"role":"user","content":json.dumps({"question":question,"row_count":len(record["results"]),"answer_complete":True,"rows":record["results"]},ensure_ascii=False,default=str)}]
                summary = model(messages,cfg).strip()
                if summary:
                    record["summary"] = summary[:1000]
                    record["stages"].append({"stage":"总结","source":"allowed_model","passed":True})
            except Exception as exc:
                record["stages"].append({"stage":"总结","source":"formatted_fallback","passed":False,"error":str(exc)})
    except Exception as exc:
        record["error"] = str(exc)
        record["stages"].append({"stage":"错误","message":str(exc)})
    record["elapsed_seconds"] = round(time.time()-started,3)
    DATA.mkdir(exist_ok=True)
    with (DATA / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record,ensure_ascii=False,default=str)+"\n")
    return record
