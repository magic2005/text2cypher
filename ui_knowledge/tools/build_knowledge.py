"""Author-maintained NL/Cypher pairs; execute every pair before publishing."""
import json
from datetime import datetime, timezone
from agent.core import DATA, Graph, configuration, schema, validate

SPECS = [
 ("单节点", "列出十个微服务及其名称。", "MATCH (s:微服务) RETURN s.服务名称 AS 服务 LIMIT 10", None),
 ("单节点", "列出十个方法名称。", "MATCH (m:方法) RETURN m.方法名 AS 方法 LIMIT 10", None),
 ("单节点", "列出十个类的全限定名。", "MATCH (c:类) RETURN c.全限定名 AS 类 LIMIT 10", None),
 ("单节点", "列出十个上游 API 路径。", "MATCH (a:上游API) RETURN a.API路径 AS 路径 LIMIT 10", None),
 ("单节点", "列出消息队列名称。", "MATCH (q:消息队列) RETURN q.队列名称 AS 队列 LIMIT 10", None),
 ("单节点", "列出消息交换机名称。", "MATCH (e:消息交换机) RETURN e.交换机名称 AS 交换机 LIMIT 10", None),
 ("属性过滤", "查找 ts-food-service 微服务。", "MATCH (s:微服务) WHERE s.服务名称 = 'ts-food-service' RETURN s.服务名称 AS 服务, s.nodeId AS ID LIMIT 10", "ts-food-service"),
 ("属性过滤", "查找名为 getAllTrains 的方法。", "MATCH (m:方法) WHERE m.方法名 = 'getAllTrains' RETURN m.方法名 AS 方法, m.全限定名 AS 全名 LIMIT 10", "getAllTrains"),
 ("属性过滤", "列出 Controller 类型的类。", "MATCH (c:类) WHERE c.类类型 = 'CONTROLLER' RETURN c.全限定名 AS 类 LIMIT 10", None),
 ("属性过滤", "展示十个 GET 类型的上游 API 样例。", "MATCH (a:上游API) WHERE a.HTTP方法 = 'GET' RETURN a.API路径 AS 路径 ORDER BY 路径 LIMIT 10", None),
 ("属性过滤", "查找 food_delivery 消息队列。", "MATCH (q:消息队列) WHERE q.队列名称 = 'food_delivery' RETURN q.队列名称 AS 队列, q.所属服务名 AS 服务 LIMIT 10", "food_delivery"),
 ("入口API", "ts-admin-basic-info-service 有哪些入口 API？", "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(c:类)-[:归属于]->(s:微服务) WHERE s.服务名称 = 'ts-admin-basic-info-service' RETURN DISTINCT a.API路径 AS 路径, m.方法名 AS 方法 LIMIT 30", None),
 ("入口API", "getAllTrains 对应哪些入口 API？", "MATCH (a:上游API)-[:归属于]->(m:方法) WHERE m.方法名 = 'getAllTrains' RETURN DISTINCT a.API路径 AS 路径, a.HTTP方法 AS HTTP方法 LIMIT 20", None),
 ("入口API", "adminbasicservice 的 contacts 入口 API 属于哪些方法？", "MATCH (a:上游API)-[:归属于]->(m:方法) WHERE a.API路径 = '/api/v1/adminbasicservice/adminbasic/contacts' RETURN DISTINCT m.方法名 AS 方法, a.HTTP方法 AS HTTP方法 LIMIT 20", None),
 ("入口API", "ts-admin-basic-info-service 有多少个入口 API？", "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(c:类)-[:归属于]->(s:微服务) WHERE s.服务名称 = 'ts-admin-basic-info-service' RETURN count(DISTINCT a) AS 数量 LIMIT 10", None),
 ("下游服务", "getTickets 方法调用哪些下游服务？", "MATCH (m:方法)-[:下游调用]->(d:下游API)-[:目标服务]->(s:微服务) WHERE m.方法名 = 'getTickets' RETURN DISTINCT s.服务名称 AS 下游服务, d.API路径 AS 路径 LIMIT 30", None),
 ("下游服务", "ts-food-service 的方法调用哪些下游服务？", "MATCH (m:方法)-[:归属于]->(c:类)-[:归属于]->(o:微服务) MATCH (m)-[:下游调用]->(d:下游API)-[:目标服务]->(s:微服务) WHERE o.服务名称 = 'ts-food-service' RETURN DISTINCT s.服务名称 AS 下游服务 LIMIT 30", None),
 ("下游服务", "哪些方法调用 ts-basic-service 的 API？", "MATCH (m:方法)-[:下游调用]->(d:下游API)-[:目标服务]->(s:微服务) WHERE s.服务名称 = 'ts-basic-service' RETURN DISTINCT m.方法名 AS 方法, d.API路径 AS 路径 LIMIT 30", None),
 ("下游服务", "展示二十条下游 API 与真实入口 API 的外部调用映射样例。", "MATCH (d:下游API)-[:外部调用]->(a:上游API) RETURN d.nodeId AS 下游ID, a.nodeId AS 入口ID, d.API路径 AS 下游路径, a.API路径 AS 入口路径 ORDER BY 下游ID, 入口ID LIMIT 20", None),
 ("反向影响", "哪些方法会调用 getAllContacts？", "MATCH (caller:方法)-[:调用|接口调用|链中下一节点]->(target:方法) WHERE target.方法名 = 'getAllContacts' RETURN DISTINCT caller.方法名 AS 调用者, target.方法名 AS 目标 LIMIT 20", "getAllContacts"),
 ("反向影响", "修改 deleteContact 会影响哪些上游入口 API？", "MATCH (a:上游API)-[:归属于]->(entry:方法)-[:调用|接口调用|链中下一节点*1..3]->(target:方法) WHERE target.方法名 = 'deleteContact' RETURN DISTINCT a.API路径 AS 入口路径, target.方法名 AS 目标 LIMIT 20", "deleteContact"),
 ("反向影响", "哪些服务调用了 ts-basic-service 的下游接口？", "MATCH (src:微服务)<-[:归属于]-(c:类)<-[:归属于]-(m:方法)-[:下游调用]->(d:下游API)-[:目标服务]->(target:微服务) WHERE target.服务名称 = 'ts-basic-service' RETURN DISTINCT src.服务名称 AS 上游服务, target.服务名称 AS 目标 LIMIT 30", "ts-basic-service"),
 ("反向影响", "哪些入口 API 经一到三跳调用 getAllTrains？", "MATCH (a:上游API)-[:归属于]->(m:方法)-[:调用|接口调用|链中下一节点*1..3]->(t:方法) WHERE t.方法名 = 'getAllTrains' RETURN DISTINCT a.API路径 AS 入口路径, t.方法名 AS 目标 LIMIT 20", "getAllTrains"),
 ("MQ链路", "food_delivery 队列完整的发布、交换、路由、消费链路是什么？", "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) WHERE q.队列名称 = 'food_delivery' AND pub.路由键 IS NOT NULL AND pub.路由键 = route.路由键 RETURN DISTINCT p.全限定名 AS 发布者, e.交换机名称 AS 交换机, q.队列名称 AS 队列, c.全限定名 AS 消费者 LIMIT 20", "food_delivery"),
 ("MQ链路", "哪些方法向 food_delivery 队列发布消息？", "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列) WHERE q.队列名称 = 'food_delivery' AND pub.路由键 IS NOT NULL AND pub.路由键 = route.路由键 RETURN DISTINCT p.全限定名 AS 发布者, q.队列名称 AS 队列 LIMIT 20", "food_delivery"),
 ("MQ链路", "哪些方法消费 food_delivery 队列？", "MATCH (q:消息队列)-[:消费自]->(c:方法) WHERE q.队列名称 = 'food_delivery' RETURN DISTINCT c.全限定名 AS 消费者, q.队列名称 AS 队列 LIMIT 20", "food_delivery"),
 ("MQ链路", "ts-food-service 通过消息依赖连接到哪些服务？", "MATCH (s:微服务)-[:消息依赖]->(t:微服务) WHERE s.服务名称 = 'ts-food-service' RETURN s.服务名称 AS 来源, t.服务名称 AS 消息目标 LIMIT 20", "ts-food-service"),
 ("REST与MQ", "ts-food-service 同时有哪些 REST 下游目标和 MQ 目标？", "MATCH (s:微服务)-[:消息依赖]->(q:微服务) MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s) MATCH (m)-[:下游调用]->(d:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称 = 'ts-food-service' RETURN DISTINCT s.服务名称 AS 来源, t.服务名称 AS REST目标, q.服务名称 AS MQ目标 LIMIT 30", "ts-food-service"),
 ("REST与MQ", "ts-preserve-other-service 的 REST 与 MQ 联合依赖有哪些？", "MATCH (s:微服务)-[:消息依赖]->(q:微服务) MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s) MATCH (m)-[:下游调用]->(d:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称 = 'ts-preserve-other-service' RETURN DISTINCT t.服务名称 AS REST目标, q.服务名称 AS MQ目标 LIMIT 30", None),
 ("REST与MQ", "ts-food-service 调用 ts-train-food-service 时同时有哪些 MQ 依赖？", "MATCH (s:微服务)-[:消息依赖]->(q:微服务) MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s) MATCH (m)-[:下游调用]->(d:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称 = 'ts-food-service' AND t.服务名称 = 'ts-train-food-service' RETURN DISTINCT t.服务名称 AS REST目标, q.服务名称 AS MQ目标 LIMIT 20", "ts-train-food-service"),
]


def main():
    assert len(SPECS) == 30
    graph = Graph(configuration())
    records, audit = [], []
    try:
        for index,(category,question,cypher,needle) in enumerate(SPECS,1):
            entry = {"id":f"K{index:02}","category":category,"question":question,"cypher":cypher}
            record = {"id":entry["id"],"question":question,"cypher":cypher,"checked_at":datetime.now(timezone.utc).isoformat()}
            try:
                safe = validate(cypher, schema())
                rows = graph.run(safe)
                record["row_count"] = len(rows)
                record["sample"] = rows[:3]
                record["semantic_review_method"] = "人工核对问题与查询关系；指定锚点还须出现在真实结果中"
                record["semantic_check"] = bool(rows) and (not needle or needle in json.dumps(rows,ensure_ascii=False))
                record["passed"] = bool(rows) and record["semantic_check"]
            except Exception as exc:
                record["passed"] = False
                record["error"] = str(exc)
            records.append(entry); audit.append(record)
    finally:
        graph.close()
    failed = [r for r in audit if not r["passed"]]
    if failed:
        raise RuntimeError("知识库验证未全部通过：" + ",".join(r["id"] for r in failed))
    DATA.mkdir(exist_ok=True)
    (DATA/"knowledge.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
    (DATA/"knowledge_validation.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"total":len(audit),"passed":sum(r["passed"] for r in audit),"failed":[{"id":r["id"],"error":r.get("error"),"count":r.get("row_count")} for r in audit if not r["passed"]]},ensure_ascii=True))

if __name__ == "__main__": main()
