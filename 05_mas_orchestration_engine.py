"""Multi-agent orchestration engine for building energy retrofit (BER)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from importlib import import_module

# Import the predefined BI-KG retrieval functions and tool schemas.
_retrieval = import_module("03_building_graph_retrieval_tools")
GraphRetrievalTools = _retrieval.GraphRetrievalTools
TOOL_SCHEMAS = _retrieval.TOOL_SCHEMAS
dispatch = _retrieval.dispatch

TEMPERATURE = 0.2
TOP_P = 0.9
MAX_REACT_ITERATIONS = 3
TOP_K = 3
ROUTES = {
    "BI-KG_ONLY": (True, False),
    "RC-KG_ONLY": (False, True),
    "DUAL_TRACK": (True, True),
}
AGENTS = ("Task Manager", "Building Analyst", "Regulation Specialist", "Retrofit Consultant")
REGULATORY_TOOL = {
    "type": "function", "function": {
        "name": "Retrieve_regulatory_evidence",
        "description": "Search the RC-KG vector store, then traverse parent standards and cited documents. Returns at most three segments per search.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"], "additionalProperties": False},
    },
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _parse_object(value: str | None) -> dict:
    if not isinstance(value, str):
        raise ValueError("Expected a JSON object from the model")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object from the model")
    return result


def load_agent_prompts(path: str | Path) -> dict[str, str]:
    """Load the four agent prompt templates from the Markdown file."""
    content = Path(path).read_text(encoding="utf-8")
    parts = re.split(r"(?m)^##\s+", content)
    prompts = {}
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        for name in AGENTS:
            if re.match(rf"\d+\.\s+{re.escape(name)}\s*\(", heading):
                if name in prompts:
                    raise ValueError(f"Duplicate prompt: {name}")
                prompts[name] = body.strip().removesuffix("---").strip()
    if set(prompts) != set(AGENTS):
        raise ValueError("Prompt file must contain all four agent sections")
    return prompts


@dataclass
class APICallMetrics:
    """Record usage and latency for one LLM API invocation."""
    call_id: int
    agent: str
    stage: str
    retrieval_round: int | None = None
    successful: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_s: float = 0.0
    finish_reason: str | None = None
    error_type: str | None = None

    def export(self) -> dict:
        result = asdict(self)
        result["token_consumption"] = (
            self.input_tokens + self.output_tokens
            if self.input_tokens is not None and self.output_tokens is not None
            else None
        )
        return result


@dataclass
class RunMetrics:
    """Record per-query and per-agent computational costs."""
    api_calls: int = 0
    successful_api_calls: int = 0
    usage_reported_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    response_time_s: float = 0.0
    call_records: list[APICallMetrics] = field(default_factory=list)

    def export(self) -> dict:
        result = asdict(self)
        result["token_consumption"] = self.input_tokens + self.output_tokens
        result["usage_complete"] = self.api_calls == self.usage_reported_calls
        result["call_records"] = [c.export() for c in sorted(self.call_records, key=lambda c: c.call_id)]
        by_agent = {}
        for agent in AGENTS:
            calls = [c for c in self.call_records if c.agent == agent]
            reported = [c for c in calls if c.input_tokens is not None and c.output_tokens is not None]
            input_tokens = sum(c.input_tokens for c in reported)
            output_tokens = sum(c.output_tokens for c in reported)
            by_agent[agent] = {
                "api_calls": len(calls),
                "successful_api_calls": sum(c.successful for c in calls),
                "usage_reported_calls": len(reported),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "token_consumption": input_tokens + output_tokens,
                "api_duration_s": math.fsum(c.duration_s for c in calls),
                "usage_complete": len(calls) == len(reported),
            }
        result["by_agent"] = by_agent
        return result


# Graph-enhanced regulatory knowledge retrieval (Section 3.3.3).
def graph_enhanced_regulatory_retrieval(driver, query_vector, segments, database="neo4j", top_k=3):
    """Local cosine ranking plus parent-standard and cited-document traversal.

    Segment objects: {segment_id, clause_id, text, embedding}. Neo4j RC-KG labels
    use CodeClause, CodeTitle and CitedDocument with `id` and `label`.
    Clause text is stored in CodeClause.text; cited-document metadata preserves
    the source reference and its identifier.
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if not isinstance(segments, list):
        raise ValueError("Segments must be a list")
    segment_ids = set()
    for segment in segments:
        if not isinstance(segment, dict) or not all(k in segment for k in ("segment_id", "clause_id", "text", "embedding")):
            raise ValueError("Invalid segment record")
        sid = segment['segment_id']
        if not isinstance(sid, str) or not sid or sid in segment_ids:
            raise ValueError("Segment identifiers must be unique nonempty strings")
        if not isinstance(segment['clause_id'], str) or not segment['clause_id']:
            raise ValueError("A segment requires its source clause identifier")
        segment_ids.add(sid)

    def cosine(a,b):
        if len(a)!=len(b) or not a:
            raise ValueError("Embedding dimensions do not match")
        a, b = list(map(float, a)), list(map(float, b))
        if not all(math.isfinite(x) for x in a + b):
            raise ValueError("Invalid embedding")
        norm_a, norm_b = math.hypot(*a), math.hypot(*b)
        if norm_a == 0 or norm_b == 0:
            raise ValueError("Zero-norm embedding")
        score=math.fsum((x/norm_a)*(y/norm_b) for x,y in zip(a,b))
        if not math.isfinite(score):
            raise ValueError("Invalid embedding")
        return score
    ranked=sorted(((cosine(query_vector,s['embedding']),s) for s in segments),key=lambda item:(-item[0],item[1]['segment_id']))[:top_k]
    result=[]
    with driver.session(database=database) as session:
        for score,s in ranked:
            rows=session.execute_read(lambda tx: [r.data() for r in tx.run("""MATCH (c:CodeClause {id:$id})
                MATCH (p:CodeTitle)-[:Includes]->(c)
                OPTIONAL MATCH (c)-[:Cites]->(d:CitedDocument)
                RETURN c.id AS clause_id,c.text AS clause_text,p.id AS code_id,p.label AS code_title,
                       collect(DISTINCT {id:d.id,label:d.label}) AS cited_documents""",id=s['clause_id'])])
            for row in rows:
                row['cited_documents']=[x for x in row['cited_documents'] if x['id'] is not None]
            matched = len(rows) == 1 and bool(rows[0].get('code_id')) and isinstance(rows[0].get('clause_text'), str) and bool(rows[0]['clause_text'].strip())
            result.append({"segment_id":s['segment_id'],"clause_id":s['clause_id'],"text":s['text'],
                           "cosine_similarity":score,"graph_context":rows,"status":"matched" if matched else "unmapped_clause"})
    return result


class RegulatoryRetrievalService:
    """Retrieve regulatory segments and their associated Neo4j graph context.

    Query encoding follows the embedding configuration of the stored segments,
    including preprocessing, tokenizer, model revision, pooling and normalization.
    """
    def __init__(self, driver, segments: list[dict], embed_query: Callable,
                 database: str = "neo4j"):
        if not isinstance(segments, list) or not segments:
            raise ValueError("The RC-KG segment store must be a nonempty JSON list")
        ids = set()
        for segment in segments:
            if not isinstance(segment, dict) or not all(
                k in segment for k in ("segment_id", "clause_id", "text", "embedding")
            ):
                raise ValueError("A regulatory segment lacks required fields")
            sid = segment["segment_id"]
            if not isinstance(sid, str) or not sid or sid in ids:
                raise ValueError("Segment IDs must be unique nonempty strings")
            ids.add(sid)
        self.driver = driver
        self.segments = segments
        self.embed_query = embed_query
        self.database = database

    def search(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Regulatory query must be nonempty text")
        vector = self.embed_query(query)
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        return graph_enhanced_regulatory_retrieval(
            self.driver, list(vector), self.segments, database=self.database, top_k=TOP_K,
        )


class MASOrchestrationEngine:
    """Coordinate task planning, knowledge retrieval and decision synthesis.

    The engine receives an Azure OpenAI client, deployment name, retrieval
    services and agent prompts. Each user query has independent execution state
    and performance metrics. The interface supports Streamlit, command-line
    interaction and benchmark evaluation; callers manage service connections.
    """
    def __init__(self, client, deployment: str,
                 building_tools: GraphRetrievalTools | None = None,
                 regulatory_service: RegulatoryRetrievalService | None = None,
                 prompts_path: str | Path | None = None):
        if not deployment:
            raise ValueError("An Azure deployment name is required")
        # Count one SDK invocation per API attempt, with automatic retries disabled.
        self.client = client.with_options(max_retries=0) if callable(getattr(client, "with_options", None)) else client
        self.deployment = deployment
        self.building_tools = building_tools
        self.regulatory_service = regulatory_service
        self.prompts = load_agent_prompts(prompts_path or Path(__file__).with_name("04_prompts_mas_agents.md"))

    def run(self, user_prompt: str, *, include_trace: bool = False) -> dict:
        """Execute the selected reasoning pathway and return its decision report.

        RT is end-to-end wall time. AC counts attempted LLM API invocations,
        and TC sums reported input and output tokens. Per-call and per-agent
        usage records accompany the aggregate metrics. The retrieval trace
        optionally includes the task plan and tool observations.
        """
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("User prompt must be nonempty text")
        run = _QueryRun(self, user_prompt)
        started = perf_counter()
        result = {"status": "failed", "route": None, "answer_markdown": "",
                  "supporting_bi_node_ids": [], "supporting_rc_clause_ids": [],
                  "unresolved_items": []}
        try:
            plan = run.plan()
            result["route"] = plan["route"]
            bi_active, rc_active = ROUTES[plan["route"]]
            # Only activate the requested track(s). Both searches start before
            # either future is awaited, so dual-track execution is parallel.
            jobs = {}
            with ThreadPoolExecutor(max_workers=2) as pool:
                if bi_active:
                    jobs["bi"] = pool.submit(run.building, plan["building_query"])
                if rc_active:
                    jobs["rc"] = pool.submit(run.regulatory, plan["regulatory_query"])
                evidence = {key: future.result() for key, future in jobs.items()}
            result.update(run.synthesize(plan, evidence))
            result["status"] = "complete" if all(e["status"] == "complete" for e in evidence.values()) else "partial"
            result["track_status"] = {k: v["status"] for k, v in evidence.items()}
            result["react_iterations"] = {k: v["iterations"] for k, v in evidence.items()}
            if include_trace:
                result["task_plan"] = plan
                result["retrieval_trace"] = evidence
        except Exception as exc:
            # Avoid leaking API keys, database details or hospital records in errors.
            result["error"] = f"Orchestration failed ({type(exc).__name__}); inspect authorized local diagnostics."
        finally:
            run.metrics.response_time_s = perf_counter() - started
            result["metrics"] = run.metrics.export()
        return result


class _QueryRun:
    def __init__(self, engine: MASOrchestrationEngine, user_prompt: str):
        self.engine = engine
        self.user_prompt = user_prompt
        self.metrics = RunMetrics()
        self.lock = Lock()

    def _call(self, messages, *, agent, stage, retrieval_round=None, tools=None, json_mode=False):
        kwargs = dict(model=self.engine.deployment, messages=messages,
                      temperature=TEMPERATURE, top_p=TOP_P)
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        with self.lock:
            self.metrics.api_calls += 1
            record = APICallMetrics(self.metrics.api_calls, agent, stage, retrieval_round)
            self.metrics.call_records.append(record)
        started = perf_counter()
        try:
            response = self.engine.client.chat.completions.create(**kwargs)
            with self.lock:
                record.successful = True
                self.metrics.successful_api_calls += 1
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "prompt_tokens", None)
                output_tokens = getattr(usage, "completion_tokens", None)
                if (type(input_tokens) is int and input_tokens >= 0
                        and type(output_tokens) is int and output_tokens >= 0):
                    record.input_tokens = input_tokens
                    record.output_tokens = output_tokens
                    self.metrics.input_tokens += input_tokens
                    self.metrics.output_tokens += output_tokens
                    self.metrics.usage_reported_calls += 1
            choice = response.choices[0]
            record.finish_reason = choice.finish_reason
            if choice.finish_reason in {"length", "content_filter"}:
                raise ValueError("Incomplete model response")
            # A terminal answer is the validation step, not another retrieval round.
            if stage == "retrieval_or_validation" and not getattr(choice.message, "tool_calls", None):
                record.stage = "evidence_validation"
                record.retrieval_round = max(0, retrieval_round - 1)
            elif stage == "retrieval_or_validation":
                record.stage = "retrieval"
            return choice.message
        except Exception as exc:
            record.error_type = type(exc).__name__
            raise
        finally:
            record.duration_s = perf_counter() - started

    def _messages(self, name, payload):
        return [{"role": "system", "content": self.engine.prompts[name]},
                {"role": "user", "content": _json(payload)}]

    def plan(self):
        messages = self._messages("Task Manager", {"user_request": self.user_prompt})
        plan = _parse_object(self._call(messages, agent="Task Manager", stage="task_planning", json_mode=True).content)
        route = plan.get("route")
        if route not in ROUTES:
            raise ValueError("Unrecognized task route")
        bi, rc = ROUTES[route]
        if not isinstance(plan.get("global_context"), dict):
            raise ValueError("Missing global context")
        plan["global_context"]["original_prompt"] = self.user_prompt
        bq, rq = plan.get("building_query"), plan.get("regulatory_query")
        if bi and (not isinstance(bq, dict) or not isinstance(bq.get("tasks"), list) or not bq["tasks"]):
            raise ValueError("Invalid building query")
        if rc and (not isinstance(rq, str) or not rq.strip()):
            raise ValueError("Invalid regulatory query")
        plan["building_query"] = bq if bi else None
        plan["regulatory_query"] = rq if rc else None
        return plan

    def building(self, query):
        def execute(name, args):
            if self.engine.building_tools is None:
                return {"error": "BI-KG service is not configured"}
            return dispatch(self.engine.building_tools, name, args)
        return self._retrieve("Building Analyst", query, TOOL_SCHEMAS, execute, "bi")

    def regulatory(self, query):
        def execute(name, args):
            if name != "Retrieve_regulatory_evidence" or not isinstance(args, dict) or set(args) != {"query"}:
                return {"error": "Invalid regulatory retrieval request"}
            if self.engine.regulatory_service is None:
                return {"error": "RC-KG service is not configured"}
            return self.engine.regulatory_service.search(args["query"])
        return self._retrieve("Regulation Specialist", query, [REGULATORY_TOOL], execute, "rc")

    def _retrieve(self, name, query, tools, execute, track):
        messages = self._messages(name, {"assigned_query": query,
                                          "max_retrieval_rounds": MAX_REACT_ITERATIONS})
        observations, allowed_ids = [], set()
        iterations = 0
        payload = None
        try:
            # A ReAct iteration is one model-issued tool-call batch and its
            # observations. Multiple related BI-KG functions may share a round.
            # Regulatory search is limited to one top-k request per round.
            while iterations < MAX_REACT_ITERATIONS:
                message = self._call(messages, agent=name, stage="retrieval_or_validation",
                                     retrieval_round=iterations + 1, tools=tools)
                calls = list(getattr(message, "tool_calls", None) or [])
                if not calls:
                    payload = _parse_object(message.content)
                    break
                iterations += 1
                messages.append({"role": "assistant", "content": message.content,
                                 "tool_calls": [{"id": c.id, "type": "function",
                                    "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
                for index, call in enumerate(calls):
                    try:
                        args = _parse_object(call.function.arguments)
                        if track == "rc" and index > 0:
                            outcome = {"error": "One regulatory search is permitted per iteration"}
                        else:
                            outcome = execute(call.function.name, args)
                        # Test serialization before recording/feeding the output.
                        _json(outcome)
                    except Exception as exc:
                        outcome = {"error": f"Retrieval failed ({type(exc).__name__}); refine query or report missing evidence"}
                    observations.append({"iteration": iterations, "tool": call.function.name, "result": outcome})
                    if track == "bi" and isinstance(outcome, dict):
                        allowed_ids.update(x for x in outcome.get("evidence_node_ids", []) if isinstance(x, str))
                    elif track == "rc" and isinstance(outcome, list):
                        allowed_ids.update(s["clause_id"] for s in outcome if s.get("status") == "matched" and isinstance(s.get("clause_id"), str))
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(outcome)})
            if payload is None:
                # Final evidence validation/synthesis is a separate LLM call,
                # without tools, after the third retrieval round. It is not a
                # fourth retrieval iteration; its tokens and API call count.
                messages.append({"role": "user", "content": "Retrieval budget exhausted. Return the structured JSON evidence payload now. Report all unresolved information; do not request more tools."})
                payload = _parse_object(self._call(messages, agent=name, stage="evidence_validation",
                                                   retrieval_round=iterations, json_mode=True).content)
            if payload.get("status") not in {"complete", "partial", "insufficient"} or not isinstance(payload.get("evidence"), list):
                raise ValueError("Invalid evidence payload")
            key = "node_id" if track == "bi" else "clause_id"
            items = [e for e in payload["evidence"] if isinstance(e, dict) and e.get(key) in allowed_ids]
            removed = len(items) != len(payload["evidence"])
            payload["evidence"] = items
            payload["evidence_node_ids"] = sorted({e[key] for e in items})
            if not items:
                payload["status"] = "insufficient"
            elif removed and payload["status"] == "complete":
                payload["status"] = "partial"
            if removed:
                missing = payload.get("missing_information", [])
                payload["missing_information"] = (missing if isinstance(missing, list) else []) + ["One or more evidence IDs were not returned by the authorized retrieval tools."]
        except Exception as exc:
            payload = {"status": "insufficient", "evidence": [], "evidence_node_ids": [],
                       "missing_information": [f"{name} did not produce validated evidence ({type(exc).__name__})."]}
        return {**payload, "iterations": iterations, "observations": observations}

    def synthesize(self, plan, evidence):
        # Pass filtered evidence, not full raw tool output, to the consultant.
        active = {k: {x: v for x, v in e.items() if x != "observations"} for k, e in evidence.items()}
        messages = self._messages("Retrofit Consultant", {"route": plan["route"],
                                  "global_context": plan["global_context"], "retrieved_evidence": active})
        messages.append({"role": "user", "content": "Return JSON with answer_markdown (string), supporting_bi_node_ids (list), supporting_rc_clause_ids (list), and unresolved_items (list). Use only evidence from the active tracks. Do not imply a missing track was verified."})
        output = _parse_object(self._call(messages, agent="Retrofit Consultant",
                                         stage="decision_generation", json_mode=True).content)
        if not isinstance(output.get("answer_markdown"), str):
            raise ValueError("Missing decision report")
        for field, track in (("supporting_bi_node_ids", "bi"), ("supporting_rc_clause_ids", "rc")):
            authorized = set(active.get(track, {}).get("evidence_node_ids", []))
            ids = output.get(field, [])
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in authorized for i in ids):
                raise ValueError("Decision references an unsupported evidence ID")
            output[field] = list(dict.fromkeys(ids))
        unresolved = output.get("unresolved_items", [])
        if not isinstance(unresolved, list):
            raise ValueError("Invalid unresolved_items")
        for track, e in active.items():
            if e["status"] != "complete":
                unresolved.append(f"{track.upper()} retrieval status: {e['status']}")
        # Return the decision report and its linked evidence identifiers.
        return {"answer_markdown": output["answer_markdown"],
                "supporting_bi_node_ids": output["supporting_bi_node_ids"],
                "supporting_rc_clause_ids": output["supporting_rc_clause_ids"],
                "unresolved_items": unresolved}


def main():
    """Initialize retrieval services and execute the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Natural-language BER question")
    parser.add_argument("--segments", type=Path, help="RC-KG segment store (JSON list)")
    parser.add_argument("--embedding-model", help="Local SentenceTransformer model path matching the stored vectors")
    parser.add_argument("--query-prompt", help="Encoder-specific query prefix matching the document embedding configuration")
    parser.add_argument("--rc-database", default=os.getenv("RC_NEO4J_DATABASE", os.getenv("NEO4J_DATABASE", "neo4j")))
    parser.add_argument("--prompts", type=Path, default=Path(__file__).with_name("04_prompts_mas_agents.md"))
    parser.add_argument("--include-trace", action="store_true", help="Include the task plan and raw retrieval observations in output")
    args = parser.parse_args()
    if bool(args.segments) != bool(args.embedding_model):
        parser.error("--segments and --embedding-model must be supplied together")
    required = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_DEPLOYMENT"]
    missing = [x for x in required if not os.getenv(x)]
    if missing:
        parser.error("Missing environment variables: " + ", ".join(missing))
    from openai import AzureOpenAI
    client = AzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                         api_key=os.environ["AZURE_OPENAI_API_KEY"],
                         api_version=os.environ["AZURE_OPENAI_API_VERSION"],
                         timeout=120.0, max_retries=0)
    tools = None
    try:
        neo = [os.getenv(k) for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")]
        if any(neo) and not all(neo):
            parser.error("Configure NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD together")
        if all(neo):
            tools = GraphRetrievalTools(*neo, database=os.getenv("NEO4J_DATABASE", "neo4j"))
        regulatory = None
        if args.segments:
            if tools is None:
                parser.error("RC-KG traversal requires a configured Neo4j connection")
            from sentence_transformers import SentenceTransformer
            model_path = Path(args.embedding_model)
            if not model_path.is_dir():
                parser.error("Specify the local SentenceTransformer model directory")
            encoder = SentenceTransformer(str(model_path), trust_remote_code=False, local_files_only=True)
            def embed_query(text):
                return encoder.encode((args.query_prompt or "") + text, convert_to_numpy=True).tolist()
            segments = json.loads(args.segments.read_text(encoding="utf-8"))
            regulatory = RegulatoryRetrievalService(tools.driver, segments, embed_query, database=args.rc_database)
        engine = MASOrchestrationEngine(client, os.environ["AZURE_OPENAI_DEPLOYMENT"], tools, regulatory, args.prompts)
        result = engine.run(args.query, include_trace=args.include_trace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["status"] == "failed" else 0
    finally:
        if tools is not None:
            tools.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
