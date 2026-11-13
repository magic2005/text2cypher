"""Run the independent teacher-style mock exam; never used by the runtime retriever.

The inline question set is validated offline.  Neo4j and the model
configuration are imported only in ``main`` after validation, so this module
can be imported and checked without those dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from evaluation.benchmark import (
    MOCK_QUESTIONS_PATH,
    atomic_write_json,
    mock_questions_payload,
    safe_output_path,
    validate_mock_cases,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evaluation" / "mock_teacher_2026-10-14"

# These questions deliberately use entities and compositions absent from the
# project's original 30-question local evaluation. Gold queries are read-only.
CASES = [
    ("S01", "简单", "名称以 ts-admin- 开头的不同微服务共有几个？",
     "MATCH (s:微服务) WHERE s.服务名称 STARTS WITH 'ts-admin-' RETURN count(DISTINCT s.服务名称) AS 数量"),
    ("S02", "简单", "图谱中 HTTP DELETE 入口 API 有多少种不同路径？",
     "MATCH (a:上游API) WHERE a.HTTP方法='DELETE' RETURN count(DISTINCT a.API路径) AS 数量"),
    ("S03", "简单", "名为 food_delivery 的队列分别属于哪些微服务？",
     "MATCH (q:消息队列) WHERE q.队列名称='food_delivery' RETURN DISTINCT q.所属服务名 AS 服务"),
    ("S04", "简单", "请给出 ts-admin-order-service 对应的微服务节点 ID。",
     "MATCH (s:微服务) WHERE s.服务名称='ts-admin-order-service' RETURN DISTINCT s.nodeId AS 节点ID"),
    ("S05", "简单", "AdminOrderServiceImpl 实现类的全限定名是什么？",
     "MATCH (c:类) WHERE c.全限定名 ENDS WITH 'AdminOrderServiceImpl' RETURN DISTINCT c.全限定名 AS 类"),
    ("S06", "简单", "方法名为 addOrder 的代码方法分别位于哪些类？",
     "MATCH (m:方法)-[:归属于]->(c:类) WHERE m.方法名='addOrder' RETURN DISTINCT c.全限定名 AS 类"),
    ("S07", "简单", "目标服务名为 ts-station-service 的下游 API 有多少种不同路径？",
     "MATCH (d:下游API) WHERE d.目标服务名='ts-station-service' RETURN count(DISTINCT d.API路径) AS 数量"),
    ("S08", "简单", "当前图谱中消息交换机有哪些不同类型？",
     "MATCH (e:消息交换机) RETURN DISTINCT e.交换机类型 AS 类型"),
    ("S09", "简单", "图谱中记录了多少个调用点节点？",
     "MATCH (p:调用点) RETURN count(p) AS 数量"),
    ("S10", "简单", "ts-admin-order-service 的入口 API 使用了几种不同的 HTTP 方法？",
     "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务) WHERE s.服务名称='ts-admin-order-service' RETURN count(DISTINCT a.HTTP方法) AS 数量"),
    ("M01", "中等", "列出 ts-admin-order-service 对外开放的 HTTP 方法与对应路径。",
     "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务) WHERE s.服务名称='ts-admin-order-service' RETURN DISTINCT a.HTTP方法 AS 方法,a.API路径 AS 路径"),
    ("M02", "中等", "名为 addOrder 的方法直接访问了哪些下游微服务？",
     "MATCH (m:方法)-[:下游调用]->(:下游API)-[:目标服务]->(s:微服务) WHERE m.方法名='addOrder' RETURN DISTINCT s.服务名称 AS 服务"),
    ("M03", "中等", "ts-admin-basic-info-service 通过 REST 调用的目标服务有哪些？",
     "MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务), (m)-[:下游调用]->(:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称='ts-admin-basic-info-service' RETURN DISTINCT t.服务名称 AS 目标服务"),
    ("M04", "中等", "DELETE /api/v1/adminorderservice/adminorder/{orderId}/{trainNumber} 由哪个方法提供？",
     "MATCH (a:上游API)-[:归属于]->(m:方法) WHERE a.HTTP方法='DELETE' AND a.API路径='/api/v1/adminorderservice/adminorder/{orderId}/{trainNumber}' RETURN DISTINCT m.全限定名 AS 方法"),
    ("M05", "中等", "哪些微服务通过下游 API 同步依赖 ts-order-service？",
     "MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(src:微服务), (m)-[:下游调用]->(:下游API)-[:目标服务]->(dst:微服务) WHERE dst.服务名称='ts-order-service' RETURN DISTINCT src.服务名称 AS 调用服务"),
    ("M06", "中等", "面向 ts-station-service 的下游 API 分别映射到了哪些入口路径？请成对列出。",
     "MATCH (d:下游API)-[:外部调用]->(a:上游API) WHERE d.目标服务名='ts-station-service' RETURN DISTINCT d.API路径 AS 下游路径,a.API路径 AS 入口路径"),
    ("M07", "中等", "adminorder.service.AdminOrderServiceImpl 实现的是哪个接口？",
     "MATCH (c:类)-[:接口实现]->(i:类) WHERE c.全限定名='adminorder.service.AdminOrderServiceImpl' RETURN DISTINCT i.全限定名 AS 接口"),
    ("M08", "中等", "哪些方法拥有调用点节点？给出方法全限定名及调用点源码行号。",
     "MATCH (p:调用点)-[:归属于]->(m:方法) RETURN DISTINCT m.全限定名 AS 方法,p.源码行号 AS 行号"),
    ("M09", "中等", "ts-admin-order-service 每种 HTTP 方法各对应多少个不同的入口 API？",
     "MATCH (a:上游API)-[:归属于]->(m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务) WHERE s.服务名称='ts-admin-order-service' RETURN a.HTTP方法 AS HTTP方法,count(DISTINCT a.nodeId) AS 数量 ORDER BY HTTP方法"),
    ("M10", "中等", "ts-delivery-service 持有的消息队列叫什么，哪个方法负责消费？",
     "MATCH (q:消息队列)-[:消费自]->(m:方法) WHERE q.所属服务名='ts-delivery-service' RETURN DISTINCT q.队列名称 AS 队列,m.全限定名 AS 消费方法"),
    ("M11", "中等", "哪些队列名称被至少两个不同微服务使用？",
     "MATCH (q:消息队列) WITH q.队列名称 AS 名称,count(DISTINCT q.所属服务名) AS 服务数 WHERE 服务数>=2 RETURN 名称"),
    ("M12", "中等", "ts-admin-order-service 中哪些方法发起下游调用？列出方法和目标服务。",
     "MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务), (m)-[:下游调用]->(:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称='ts-admin-order-service' RETURN DISTINCT m.全限定名 AS 方法,t.服务名称 AS 目标服务"),
    ("H01", "复杂", "从 ts-admin-order-service 的入口 API 经过控制器与实现方法，最终会访问哪些下游服务？列出入口路径、实现方法和目标服务。",
     "MATCH (a:上游API)-[:归属于]->(entry:方法)-[:接口调用]->(impl:方法)-[:下游调用]->(:下游API)-[:目标服务]->(target:微服务), (entry)-[:归属于]->(:类)-[:归属于]->(src:微服务) WHERE src.服务名称='ts-admin-order-service' RETURN DISTINCT a.API路径 AS 入口路径,impl.全限定名 AS 实现方法,target.服务名称 AS 目标服务"),
    ("H02", "复杂", "哪些入口路径能在不超过两跳的方法调用内到达一个同步请求 ts-order-service 的方法？",
     "MATCH (a:上游API)-[:归属于]->(entry:方法)-[:调用*0..2]->(caller:方法)-[:下游调用]->(:下游API)-[:目标服务]->(t:微服务) WHERE t.服务名称='ts-order-service' RETURN DISTINCT a.API路径 AS 入口路径"),
    ("H03", "复杂", "从 AdminBasicInfoController.getAllStations 的入口开始，经过接口调用后到 ts-station-service 的完整 REST 路径是什么？列出入口路径、实现方法和下游路径。",
     "MATCH (a:上游API)-[:归属于]->(entry:方法)-[:接口调用]->(impl:方法)-[:下游调用]->(d:下游API)-[:目标服务]->(t:微服务) WHERE entry.全限定名='adminbasic.controller.AdminBasicInfoController.getAllStations' AND t.服务名称='ts-station-service' RETURN DISTINCT a.API路径 AS 入口路径,impl.全限定名 AS 实现方法,d.API路径 AS 下游路径"),
    ("H04", "复杂", "调用 ts-station-service 的下游 API 在图谱中映射到哪些真实入口 API？同时指出发起调用的方法。",
     "MATCH (m:方法)-[:下游调用]->(d:下游API)-[:外部调用]->(a:上游API), (d)-[:目标服务]->(t:微服务) WHERE t.服务名称='ts-station-service' RETURN DISTINCT m.全限定名 AS 调用方法,d.API路径 AS 下游路径,a.API路径 AS 入口路径"),
    ("H05", "复杂", "food_delivery 消息从哪个发布方法真正送达 ts-delivery-service 的哪个消费方法？只保留发布键与绑定键匹配的完整链路，并给出路由键。",
     "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) WHERE q.队列名称='food_delivery' AND q.所属服务名='ts-delivery-service' AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 RETURN DISTINCT p.全限定名 AS 发布方法,c.全限定名 AS 消费方法,pub.路由键 AS 路由键"),
    ("H06", "复杂", "email 消息有哪些真实可达的发布到消费闭合链路？列出发布方法、队列所属服务和消费方法，排除无消费者或路由键不一致的记录。",
     "MATCH (p:方法)-[pub:发布至]->(e:消息交换机)-[route:路由至]->(q:消息队列)-[:消费自]->(c:方法) WHERE q.队列名称='email' AND pub.路由键 IS NOT NULL AND pub.路由键=route.路由键 RETURN DISTINCT p.全限定名 AS 发布方法,q.所属服务名 AS 队列服务,c.全限定名 AS 消费方法"),
    ("H07", "复杂", "ts-admin-order-service 中哪些入口方法经接口调用后会同步请求 ts-order-service？给出 HTTP 方法、入口路径和入口方法。",
     "MATCH (a:上游API)-[:归属于]->(entry:方法)-[:接口调用]->(impl:方法)-[:下游调用]->(:下游API)-[:目标服务]->(t:微服务), (entry)-[:归属于]->(:类)-[:归属于]->(src:微服务) WHERE src.服务名称='ts-admin-order-service' AND t.服务名称='ts-order-service' RETURN DISTINCT a.HTTP方法 AS HTTP方法,a.API路径 AS 入口路径,entry.方法名 AS 入口方法"),
    ("H08", "复杂", "ts-admin-basic-info-service 发起的 REST 调用中，哪些下游 API 已映射到目标服务的入口 API？列出目标服务以及两端路径。",
     "MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(src:微服务), (m)-[:下游调用]->(d:下游API)-[:外部调用]->(a:上游API), (d)-[:目标服务]->(t:微服务) WHERE src.服务名称='ts-admin-basic-info-service' RETURN DISTINCT t.服务名称 AS 目标服务,d.API路径 AS 下游路径,a.API路径 AS 入口路径"),
]


CASES_SHA256 = hashlib.sha256(
    json.dumps([list(case) for case in CASES], ensure_ascii=False).encode("utf-8")
).hexdigest()


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(graph):
    rows = graph.run("MATCH (n) RETURN n.图谱版本 AS version, count(*) AS count")
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def validate():
    """Offline validation of the inline cases and the committed question copy."""
    problems = validate_mock_cases(CASES)
    return {
        "cases": len(CASES),
        "sha256": CASES_SHA256,
        "questions_file": str(MOCK_QUESTIONS_PATH.relative_to(ROOT)),
        "problems": problems,
    }


def ensure_questions_file():
    """Write the committed question copy only when missing; never clobber it."""
    payload = mock_questions_payload(CASES)
    if MOCK_QUESTIONS_PATH.exists():
        stored = json.loads(MOCK_QUESTIONS_PATH.read_text(encoding="utf-8"))
        if stored != payload:
            raise SystemExit(
                "已提交的模拟题 questions.json 与 CASES 不一致；"
                "请先人工同步题集，工具不会静默覆盖已归档题目"
            )
        return False
    MOCK_QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(MOCK_QUESTIONS_PATH, payload)
    return True


def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def _print_item(item_id, item):
    print(
        json.dumps(
            {
                "id": item_id,
                "gold_rows": item.get("gold_row_count", 0),
                "actual_rows": len(item.get("actual_rows") or []),
                "error": item.get("error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold-only", action="store_true", help="只执行参考查询，不调用模型"
    )
    parser.add_argument(
        "--local-oracle",
        action="store_true",
        help="用已知正确查询驱动整条流水线，不发起真实模型请求",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=_positive_int, default=30)
    parser.add_argument(
        "--output",
        default="results.json",
        help="模拟题目录内的结果文件名（默认 results.json）",
    )
    parser.add_argument(
        "--force", action="store_true", help="允许覆盖已存在的 --output 文件"
    )
    parser.add_argument(
        "--validate", action="store_true", help="只校验题集和已提交副本，不连接图谱"
    )
    args = parser.parse_args(argv)

    report = validate()
    if args.validate:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report["problems"] else 1
    if report["problems"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("模拟题集校验失败，未连接图谱。", file=sys.stderr)
        return 1

    try:
        result_path = safe_output_path(OUT, args.output, reserved=("questions.json",))
    except ValueError as error:
        parser.error(str(error))
    if (
        not args.gold_only
        and not args.local_oracle
        and result_path.name == "results.json"
    ):
        parser.error(
            "External model runs require --output to protect the local-oracle results"
        )
    if args.resume and not result_path.exists():
        parser.error(f"Cannot resume a missing output file: {result_path}")
    if not args.resume and result_path.exists() and not args.force:
        parser.error(
            f"Output already exists: {result_path.name}; use --resume or --force"
        )

    mode = (
        "gold_only"
        if args.gold_only
        else "local_oracle"
        if args.local_oracle
        else "external_model"
    )
    if args.resume:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("evaluation_mode") != mode:
            parser.error("Cannot resume a run from another evaluation mode")
        if result.get("cases_sha256") and result["cases_sha256"] != CASES_SHA256:
            parser.error("The mock question set changed since this run; cannot resume")
    else:
        result = {"evaluation_mode": mode, "cases_sha256": CASES_SHA256}

    ensure_questions_file()

    from agent.core import Graph, ask, configuration

    cfg = configuration()
    graph = Graph(cfg)
    now = _utc_now()
    result.setdefault("started_at_utc", now)
    result.setdefault("created_at_utc", now)
    result.update(
        updated_at_utc=now,
        finished_at_utc=None,
        status="running",
        planned_count=len(CASES),
        complete=False,
    )
    result.setdefault("items", [])
    done = {item["id"] for item in result["items"]}
    before = None
    after = None
    completed = False
    try:
        before = fingerprint(graph)
        if (
            result.get("graph_fingerprint_before")
            and result["graph_fingerprint_before"] != before
        ):
            result["graph_changed_between_segments"] = True
        result.setdefault("graph_fingerprint_before", before)
        result.setdefault("fingerprint_checkpoints", []).append(
            {"phase": "start", "fingerprint": before, "at_utc": _utc_now()}
        )
        count = 0
        for id_, level, question, gold_query in CASES:
            if id_ in done:
                continue
            if count >= args.limit:
                break
            gold_rows = graph.run(gold_query)
            item = {
                "id": id_,
                "level": level,
                "question": question,
                "gold_cypher": gold_query,
                "gold_rows": gold_rows,
                "gold_nonempty": bool(gold_rows),
                "gold_row_count": len(gold_rows),
                "evaluated_at_utc": _utc_now(),
            }
            if not gold_rows:
                item["error"] = (
                    "Gold query returned no rows; exclude this case from scoring."
                )
            elif not args.gold_only:
                if args.local_oracle:

                    def oracle(_messages, _cfg, query=gold_query):
                        return json.dumps(
                            {"queries": [query]}, ensure_ascii=False
                        )

                    actual = ask(question, graph, cfg, model=oracle, level=level)
                else:
                    actual = ask(question, graph, cfg, level=level)
                item.update(
                    {
                        "actual_cypher": actual.get("queries"),
                        "actual_rows": actual.get("results"),
                        "actual_summary": actual.get("summary"),
                        "answer_complete": actual.get("answer_complete"),
                        "error": actual.get("error"),
                        "stages": actual.get("stages"),
                        "truncated_subqueries": actual.get(
                            "truncated_subqueries", 0
                        ),
                        "discarded_mismatched_route_rows": actual.get(
                            "discarded_mismatched_route_rows", 0
                        ),
                        "discarded_partial_chain_rows": actual.get(
                            "discarded_partial_chain_rows", 0
                        ),
                    }
                )
            result["items"].append(item)
            done.add(id_)
            result["updated_at_utc"] = _utc_now()
            atomic_write_json(result_path, result)
            _print_item(id_, item)
            count += 1
        after = fingerprint(graph)
        stable = before == after
        result["fingerprint_checkpoints"].append(
            {"phase": "end", "fingerprint": after, "at_utc": _utc_now()}
        )
        result.update(
            graph_fingerprint_after=after,
            graph_stable=stable,
            graph_changed=bool(result.get("graph_changed_between_segments"))
            or not stable,
            status="finished" if len(done) == len(CASES) else "partial",
            complete=len(done) == len(CASES),
            updated_at_utc=_utc_now(),
            finished_at_utc=_utc_now(),
        )
        atomic_write_json(result_path, result)
        completed = True
    finally:
        graph.close()
        if not completed:
            result.update(
                status="interrupted",
                updated_at_utc=_utc_now(),
                finished_at_utc=_utc_now(),
            )
            try:
                atomic_write_json(result_path, result)
            except Exception as error:  # pragma: no cover - best effort only
                print(f"警告：中断报告未写入：{error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
