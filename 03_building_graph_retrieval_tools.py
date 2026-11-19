"""BI-KG retrieval tools for the Building Analyst."""
from __future__ import annotations
import json
import math
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from calendar import monthrange
from importlib import import_module
import re
import statistics

# Shared BI-KG semantic schema and timestamp parser.
_pipeline = import_module("01_ifc2kg_pipeline")
NODE_TYPES = _pipeline.NODE_TYPES
RELATION_SCHEMA = _pipeline.RELATION_SCHEMA
parse_timestamp = _pipeline.parse_timestamp

# Small result sets retain individual records; larger sets use full-set statistics.
DEFAULT_INLINE_RECORD_LIMIT = 12


def _record_reference(record):
    """Identify an individual observation or metering interval."""
    keys = ("record_id", "source_ref", "timestamp", "start", "end")
    return {key: record[key] for key in keys if key in record}


def _record_payload(records, limit):
    """Keep individual records for small queries and aggregate large result sets."""
    count = len(records)
    inline = count <= limit
    return {
        "records": records if inline else [],
        "record_count": count,
        "records_mode": "empty" if not count else ("individual" if inline else "aggregated"),
        "omitted_individual_record_count": 0 if inline else count,
        "first_record": _record_reference(records[0]) if records else None,
        "last_record": _record_reference(records[-1]) if records else None,
    }


def _source_references(records, limit=3):
    """Summarize source identifiers without expanding repeated references."""
    refs = sorted({str(r["source_ref"]) for r in records})
    return {"source_ref_count": len(refs), "source_refs": refs[:limit]}


def _loads(value, expected):
    if value is None:
        return expected()
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, expected):
        raise ValueError("Invalid stored node payload")
    return result


def _window(time_range, timezone="Asia/Shanghai"):
    """YYYY-MM, YYYY, or {start,end}; explicit range endpoints are exclusive at end."""
    tz = ZoneInfo(timezone)
    if isinstance(time_range, dict):
        if set(time_range) != {"start", "end"}:
            raise ValueError("Time range requires start and end")
        start = parse_timestamp(time_range["start"])
        end = parse_timestamp(time_range["end"])
    elif isinstance(time_range, str):
        pieces = time_range.split("-")
        if re.fullmatch(r"[0-9]{4}", time_range):
            y = int(pieces[0]); start = datetime(y, 1, 1, tzinfo=tz); end = datetime(y+1, 1, 1, tzinfo=tz)
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}", time_range):
            y, m = map(int, pieces); start = datetime(y,m,1,tzinfo=tz)
            end = datetime(y,m,monthrange(y,m)[1],tzinfo=tz) + timedelta(days=1)
        else:
            raise ValueError("Use YYYY-MM, YYYY, or {start,end} with timezone offsets")
    else:
        raise ValueError("Invalid time_range")
    if end.astimezone(dt_timezone.utc) <= start.astimezone(dt_timezone.utc):
        raise ValueError("time_range end must be later than start")
    return start, end


def _select_properties(properties, names):
    """Resolve property names and qualified paths while retaining source paths."""
    paths = {}

    def visit(value, path):
        if path:
            paths[".".join(path)] = value
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, path + [str(k)])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    visit(item, path + [str(index)])

    visit(properties, [])
    result, missing = {}, []
    for name in names:
        matches = ({name: paths[name]} if "." in name and name in paths else
                   {p: v for p, v in paths.items()
                    if p == name or ("." not in name and p.split(".")[-1] == name)})
        if matches:
            result[name] = matches
        else:
            missing.append(name)
    return result, missing


class GraphRetrievalTools:
    def __init__(self, uri=None, user=None, password=None, database="neo4j",
                 timezone="Asia/Shanghai", driver=None,
                 inline_record_limit=DEFAULT_INLINE_RECORD_LIMIT):
        if (not isinstance(inline_record_limit, int) or isinstance(inline_record_limit, bool)
                or inline_record_limit < 1):
            raise ValueError("inline_record_limit must be a positive integer")
        self.inline_record_limit = inline_record_limit
        if driver is None:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver, self.database, self.timezone = driver, database, timezone

    def close(self):
        self.driver.close()

    def _read(self, query, **params):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(lambda tx: [r.data() for r in tx.run(query, **params)])

    def _resolve(self, object_id, entity_type=None):
        if entity_type is not None and entity_type not in NODE_TYPES:
            raise ValueError(f"Unknown semantic node type: {entity_type}")
        if not isinstance(object_id, str) or not object_id.strip():
            raise ValueError("Object identifier must be nonempty text")
        rows = self._read("""MATCH (n:BIKGNode)
            WHERE n.id=$identifier OR n.label=$identifier
            RETURN n.id AS id, n.label AS label, n.type AS type, n.ifc_type AS ifc_type,
                   n.raw_properties AS raw_properties, n.operational_records AS operational_records
            ORDER BY n.id""", identifier=object_id)
        exact = [r for r in rows if r['id'] == object_id]
        if exact:
            if len(exact) != 1 or (entity_type is not None and exact[0]['type'] != entity_type):
                raise ValueError(f"Identifier {object_id!r} has an inconsistent semantic type or is duplicated")
            return exact[0]
        rows = [r for r in rows if entity_type is None or r['type'] == entity_type]
        if len(rows) != 1:
            raise ValueError(f"Identifier {object_id!r} matched {len(rows)} nodes; use a unique ID")
        return rows[0]

    # 1. Physical/performance attributes: exact object ID or unambiguous label.
    def Get_object_properties(self, entity_type, object_id, property_names):
        if not isinstance(property_names, list) or not property_names or not all(isinstance(x, str) and x.strip() for x in property_names):
            raise ValueError("property_names must be a nonempty list of strings")
        n = self._resolve(object_id, entity_type)
        properties = _loads(n['raw_properties'], dict)
        selected, missing = _select_properties(properties, property_names)
        return {"evidence_node_ids": [n['id']], "object_id": n['id'], "label": n['label'],
                "entity_type": n['type'], "ifc_type": n['ifc_type'], "properties": selected,
                "missing_properties": missing, "project_units": properties.get("ProjectUnits", {})}

    # 2. Relevant objects of a given category, preserving the path/evidence IDs.
    def Get_spatial_elements(self, space_id, element_category):
        if element_category not in NODE_TYPES:
            raise ValueError("Invalid element_category")
        space = self._resolve(space_id, "SpatialUnit")
        # A bounded hierarchy traversal, followed by schema-specific associations.
        rows = self._read("""MATCH (root:BIKGNode {id:$id})-[:Contains*0..8]->(s:BIKGNode:SpatialUnit)
            OPTIONAL MATCH (s)<-[r:Bounds|Serves|Monitors]-(e:BIKGNode)
            WITH root,s,e,r WHERE e IS NOT NULL AND e.type=$kind
            RETURN DISTINCT s.id AS space_id,e.id AS id,e.label AS label,e.type AS type,type(r) AS association, null AS system_id
            UNION
            MATCH (root:BIKGNode {id:$id})-[:Contains*1..8]->(e:BIKGNode:SpatialUnit)
            WHERE e.type=$kind RETURN DISTINCT root.id AS space_id,e.id AS id,e.label AS label,e.type AS type,'Contains' AS association, null AS system_id
            UNION
            MATCH (root:BIKGNode {id:$id})-[:Contains*0..8]->(s:BIKGNode:SpatialUnit)<-[:Serves]-(sys:BIKGNode:EnergySystem)-[:Includes]->(e:BIKGNode:EnergyEquipment)
            WHERE e.type=$kind RETURN DISTINCT s.id AS space_id,e.id AS id,e.label AS label,e.type AS type,'Serves/Includes' AS association,sys.id AS system_id
            UNION
            MATCH (root:BIKGNode {id:$id})-[:Contains*0..8]->(s:BIKGNode:SpatialUnit)<-[:Serves]-(sys:BIKGNode:EnergySystem)<-[:Supplies]-(e:BIKGNode:EnergySource)
            WHERE e.type=$kind RETURN DISTINCT s.id AS space_id,e.id AS id,e.label AS label,e.type AS type,'Serves/Supplies' AS association,sys.id AS system_id""", id=space['id'], kind=element_category)
        # Preserve the distinction between spatial and system associations.
        return {"space_id": space['id'], "elements": rows,
                "evidence_node_ids": sorted({space['id']} | {r['id'] for r in rows} | {r['space_id'] for r in rows} | {r['system_id'] for r in rows if r.get('system_id')}),
                "scope_note": "Serves/Includes and Serves/Supplies identify system-level associations; room-level location and metering scope require their corresponding records."}

    # 3. Parameterized relationship retrieval within the semantic schema.
    def Get_object_relationships(self, object_id, relation_type, target_type):
        if relation_type not in RELATION_SCHEMA or target_type not in NODE_TYPES:
            raise ValueError("Invalid relationship or target type")
        n = self._resolve(object_id)
        rows = self._read("""MATCH (a:BIKGNode {id:$id})-[r]-(b:BIKGNode)
            WHERE type(r)=$rel AND b.type=$kind
            RETURN DISTINCT b.id AS id,b.label AS label,b.type AS type,type(r) AS relationship,
                   CASE WHEN startNode(r)=a THEN 'outgoing' ELSE 'incoming' END AS direction,
                   r.provenance AS provenance""", id=n['id'], rel=relation_type, kind=target_type)
        return {"object_id": n['id'], "relationships": rows,
                "evidence_node_ids": sorted({n['id']} | {r['id'] for r in rows})}

    # 4. Time-bounded IEQ observations, resolved through Monitors relationships.
    def Get_ieq_records(self, space_id, indicator, time_range):
        n = self._resolve(space_id, "SpatialUnit")
        start, end = _window(time_range, self.timezone)
        rows = self._read("""MATCH (root:BIKGNode:SpatialUnit {id:$id})-[:Contains*0..8]->(s:BIKGNode:SpatialUnit)
            MATCH (s)<-[:Monitors]-(n:BIKGNode:MonitoringDevice)
            RETURN n.id AS id,n.operational_records AS records,collect(DISTINCT s.id) AS monitored_space_ids""", id=n['id'])
        records = self._observations(rows, "ieq", indicator, start, end)
        active = {r['node_id'] for r in records}
        associations = [{"node_id": r['id'], "monitored_space_ids": r['monitored_space_ids']}
                        for r in rows if r['id'] in active]
        return {"space_id": n['id'], "indicator": indicator,
                "start": start.isoformat(), "end": end.isoformat(),
                **_record_payload(records, self.inline_record_limit),
                "statistics_by_unit": self._summarize(records),
                "monitoring_associations": associations,
                "evidence_node_ids": sorted({n['id']} | active | {sid for a in associations for sid in a['monitored_space_ids']}),
                "status": "available" if records else "no_matching_records"}

    # 5. Equipment operating logs, retaining timestamp, unit and source reference.
    def Get_equipment_logs(self, equipment_id, parameter, time_range):
        n = self._resolve(equipment_id, "EnergyEquipment")
        start, end = _window(time_range, self.timezone)
        records = self._observations([{"id": n['id'], "records": n['operational_records']}], "equipment", parameter, start, end)
        return {"equipment_id": n['id'], "parameter": parameter,
                "start": start.isoformat(), "end": end.isoformat(),
                **_record_payload(records, self.inline_record_limit),
                "statistics_by_unit": self._summarize(records, include_changes=True),
                "evidence_node_ids": [n['id']], "status": "available" if records else "no_matching_records"}

    @staticmethod
    def _observations(rows, kind, indicator, start, end):
        if not isinstance(indicator, str) or not indicator.strip():
            raise ValueError("Indicator must be nonempty text")
        start_utc, end_utc = (t.astimezone(dt_timezone.utc) for t in (start, end))
        seen = {}
        for row in rows:
            for r in _loads(row['records'], list):
                if r['kind'] != kind or r['indicator'] != indicator:
                    continue
                if r['node_id'] != row['id']:
                    raise ValueError(f"Operational record assigned to incorrect node: {r['record_id']}")
                if start_utc <= parse_timestamp(r['timestamp']).astimezone(dt_timezone.utc) < end_utc:
                    if not math.isfinite(float(r['value'])):
                        raise ValueError(f"Invalid observation: {r['record_id']}")
                    old = seen.setdefault(r['record_id'], r)
                    if old != r:
                        raise ValueError(f"Conflicting record {r['record_id']}")
        return sorted(seen.values(), key=lambda r: (parse_timestamp(r['timestamp']).astimezone(dt_timezone.utc), r['record_id']))

    @staticmethod
    def _summarize(records, include_changes=False):
        """Calculate full-set statistics independently by object, indicator and unit."""
        groups = {}
        for r in records:
            groups.setdefault((r['node_id'], r['indicator'], r['unit']), []).append(r)
        summaries = []
        for (node_id, indicator, unit), group in sorted(groups.items()):
            group.sort(key=lambda r: (parse_timestamp(r['timestamp']).astimezone(dt_timezone.utc),
                                      r['record_id']))
            values = [float(r['value']) for r in group]
            low = min(group, key=lambda r: float(r['value']))
            high = max(group, key=lambda r: float(r['value']))
            summary = {
                "node_id": node_id, "indicator": indicator, "unit": unit,
                "count": len(values), "mean": statistics.mean(values),
                "minimum": min(values), "maximum": max(values),
                "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
                "first_record": _record_reference(group[0]),
                "last_record": _record_reference(group[-1]),
                "minimum_record": _record_reference(low),
                "maximum_record": _record_reference(high),
                **_source_references(group),
            }
            if include_changes:
                # Count differences between successive recorded values, not inferred control actions.
                by_time = {}
                for r in group:
                    t = parse_timestamp(r['timestamp']).astimezone(dt_timezone.utc)
                    by_time.setdefault(t, set()).add(float(r['value']))
                ambiguous = any(len(v) != 1 for v in by_time.values())
                sequence = [next(iter(v)) for _, v in sorted(by_time.items())] if not ambiguous else []
                summary["observed_value_changes"] = (
                    sum(a != b for a, b in zip(sequence, sequence[1:]))
                    if len(sequence) > 1 else None
                )
                summary["change_count_basis"] = (
                    "conflicting_values_at_same_timestamp" if ambiguous else
                    "successive_recorded_values"
                )
            summaries.append(summary)
        return summaries

    # 6. Energy consumption retrieval and interval-based aggregation in kWh.
    def Get_energy_consumption(self, energy_source_id, metering_scope, time_range):
        n = self._resolve(energy_source_id, "EnergySource")
        start, end = _window(time_range, self.timezone)
        return self._aggregate_energy(n, metering_scope, start, end, self.inline_record_limit)

    @staticmethod
    def _aggregate_energy(n, metering_scope, start, end, inline_record_limit=DEFAULT_INLINE_RECORD_LIMIT):
        records = {}
        window_start, window_end = start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)
        if not isinstance(metering_scope, str) or not metering_scope.strip():
            raise ValueError("An exact metering_scope is required")
        for r in _loads(n['operational_records'], list):
            if r['kind'] != 'energy' or r['metering_scope'] != metering_scope:
                continue
            if r['node_id'] != n['id']:
                raise ValueError(f"Energy record assigned to incorrect node: {r['record_id']}")
            a, b = (parse_timestamp(r[k]).astimezone(dt_timezone.utc) for k in ('start', 'end'))
            if a >= b:
                raise ValueError(f"Invalid energy interval: {r['record_id']}")
            if a < window_end and b > window_start:
                old = records.setdefault(r['record_id'], r)
                if old != r:
                    raise ValueError(f"Conflicting energy record {r['record_id']}")
        ordered = sorted(records.values(), key=lambda r: (parse_timestamp(r['start']).astimezone(dt_timezone.utc), r['record_id']))
        if len({r['indicator'] for r in ordered}) > 1:
            raise ValueError('Mixed energy indicators in the same metering scope; normalize records first')
        cursor = window_start
        gaps, overlaps, boundary = [], [], []
        for r in ordered:
            a, b = (parse_timestamp(r[k]).astimezone(dt_timezone.utc) for k in ('start', 'end'))
            if r['unit'] != 'kWh' or float(r['value']) < 0 or not math.isfinite(float(r['value'])):
                raise ValueError("Expected nonnegative interval consumption in kWh")
            if a < window_start or b > window_end:
                boundary.append(r['record_id']); continue
            if a < cursor:
                overlaps.append(r['record_id'])
            elif a > cursor:
                gaps.append([cursor.astimezone(start.tzinfo).isoformat(), a.astimezone(start.tzinfo).isoformat()])
            cursor = max(cursor,b)
        if cursor < window_end:
            gaps.append([cursor.astimezone(start.tzinfo).isoformat(), end.isoformat()])
        complete = bool(ordered) and not (gaps or overlaps or boundary)
        # Aggregate consumption over complete, non-overlapping intervals.
        return {"energy_source_id": n['id'], "metering_scope": metering_scope,
                "start": start.isoformat(), "end": end.isoformat(), "unit": "kWh",
                "consumption": round(math.fsum(float(r['value']) for r in ordered), 8) if complete else None,
                "status": "complete" if complete else "incomplete_or_overlapping_records",
                "indicator": ordered[0]['indicator'] if ordered else None,
                **_record_payload(ordered, inline_record_limit),
                **_source_references(ordered),
                "gap_count": len(gaps), "gaps": gaps[:inline_record_limit],
                "overlap_count": len(overlaps), "overlapping_record_ids": overlaps[:inline_record_limit],
                "boundary_count": len(boundary), "boundary_record_ids": boundary[:inline_record_limit],
                "evidence_node_ids": [n['id']] if ordered else []}


# Azure OpenAI Function Calling definitions for the Building Analyst.
_DEFINITIONS = {
    "Get_object_properties": ("Retrieve selected physical and performance properties (including source paths).", {"entity_type":"string","object_id":"string","property_names":"array"}),
    "Get_spatial_elements": ("Retrieve spatially associated building objects and equipment.", {"space_id":"string","element_category":"string"}),
    "Get_object_relationships": ("Retrieve a specified semantic relationship and target category.", {"object_id":"string","relation_type":"string","target_type":"string"}),
    "Get_ieq_records": ("Retrieve IEQ measurements and full-set per-sensor statistics. Large result sets return aggregates; use narrower periods for individual observations.", {"space_id":"string","indicator":"string","time_range":"range"}),
    "Get_equipment_logs": ("Retrieve equipment logs, full-set descriptive statistics and observed changes between successive recorded values. Use narrower periods for individual observations.", {"equipment_id":"string","parameter":"string","time_range":"range"}),
    "Get_energy_consumption": ("Retrieve interval energy consumption and coverage diagnostics in an exact scope and period. Aggregate all valid intervals before returning results.", {"energy_source_id":"string","metering_scope":"string","time_range":"range"}),
}

def _parameter_schema(key, kind):
    if kind == "array":
        return {"type":"array","items":{"type":"string"},"minItems":1}
    if kind == "range":
        return {"description":"YYYY-MM or YYYY, or a start/end ISO-8601 interval with timezone offsets.",
                "anyOf":[{"type":"string"},{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"}},"required":["start","end"],"additionalProperties":False}]}
    if key in {"entity_type","element_category","target_type"}:
        return {"type":"string","enum":sorted(NODE_TYPES)}
    if key == "relation_type":
        return {"type":"string","enum":sorted(RELATION_SCHEMA)}
    return {"type":kind}

TOOL_SCHEMAS = [{"type":"function","function":{"name":name,"description":description,
                 "parameters":{"type":"object","properties":{key:_parameter_schema(key,kind) for key,kind in fields.items()},
                               "required":list(fields),"additionalProperties":False}}}
                for name,(description,fields) in _DEFINITIONS.items()]


def dispatch(tools, name, arguments):
    """Validate the tool name/argument keys; safe errors can be used by ReAct to refine queries."""
    if name not in _DEFINITIONS:
        return {"error":"Unknown tool"}
    try:
        args = json.loads(arguments) if isinstance(arguments,str) else arguments
        if not isinstance(args,dict) or set(args) != set(_DEFINITIONS[name][1]):
            raise ValueError("Arguments do not match the tool signature")
        for key, kind in _DEFINITIONS[name][1].items():
            value = args[key]
            if kind == "string" and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} must be nonempty text")
            if kind == "array" and (not isinstance(value, list) or not value):
                raise ValueError(f"{key} must be a nonempty list")
            if kind == "range" and not isinstance(value, (str, dict)):
                raise ValueError(f"{key} must specify a time range")
        return getattr(tools,name)(**args)
    except (ValueError, TypeError, KeyError) as exc:
        return {"error":str(exc)}
