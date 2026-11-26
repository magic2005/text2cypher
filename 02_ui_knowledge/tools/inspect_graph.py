"""Print small, credential-free samples of candidate query paths."""
import json
import os
import re
import sys
import pymupdf
from neo4j import GraphDatabase, READ_ACCESS

page = pymupdf.open(os.environ["ASSIGNMENT_PDF"])[13].get_text()
password = re.search(r"密码[:：]\s*([^\s]+)", page).group(1)
driver = GraphDatabase.driver("bolt://62.234.93.223:30005", auth=("neo4j",password))
queries = {
 "services": "MATCH (s:微服务) RETURN s.服务名称 AS name LIMIT 10",
 "methods": "MATCH (m:方法)-[:归属于]->(c:类)-[:归属于]->(s:微服务) RETURN m.方法名 AS method, c.简单名 AS class, s.服务名称 AS service LIMIT 10",
 "api": "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(c:类)-[:归属于]->(s:微服务) RETURN a.API路径 AS path,m.方法名 AS method,s.服务名称 AS service LIMIT 10",
 "downstream": "MATCH (m:方法)-[:下游调用]->(d:下游API)-[:目标服务]->(s:微服务) RETURN m.方法名 AS method,d.API路径 AS path,s.服务名称 AS service LIMIT 10",
 "reverse": "MATCH (a:上游API)-[:归属于]->(m:方法)-[:调用*1..3]->(t:方法) RETURN a.API路径 AS path,m.方法名 AS entry,t.方法名 AS target LIMIT 10",
 "mq": "MATCH (p:方法)-[:发布至]->(e:消息交换机)-[:路由至]->(q:消息队列)-[:消费自]->(c:方法) RETURN p.方法名 AS publisher,e.交换机名称 AS exchange,q.队列名称 AS queue,c.方法名 AS consumer LIMIT 10",
 "mqdep": "MATCH (a:微服务)-[:消息依赖]->(b:微服务) RETURN a.服务名称 AS source,b.服务名称 AS target LIMIT 10",
 "rpc": "MATCH (d:下游API)-[:外部调用]->(a:上游API) RETURN d.API路径 AS downstream,a.API路径 AS upstream LIMIT 10",
 "hybrid": "MATCH (s:微服务)-[:消息依赖]->(q:微服务) MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s) MATCH (m)-[:下游调用]->(d:下游API)-[:目标服务]->(t:微服务) RETURN DISTINCT s.服务名称 AS source,t.服务名称 AS rpcTarget,q.服务名称 AS mqTarget LIMIT 10",
}
with driver.session(default_access_mode=READ_ACCESS) as session:
 for name,q in queries.items():
  try: print(json.dumps({"name":name,"rows":[dict(r) for r in session.run(q)]},ensure_ascii=True))
  except Exception as exc: print(json.dumps({"name":name,"error":str(exc)[:200]}))
driver.close()
