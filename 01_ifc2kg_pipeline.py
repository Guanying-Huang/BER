"""IFC2KG pipeline for building energy retrofit (BER).

BI-KG construction following the semantic schema in Fig. 2 and the
workflow in Sections 3.2.1 and 4.1.2: IFC entity mapping, attribute
extraction, semantic relationship mapping, and operational data integration.
Run `python 01_ifc2kg_pipeline.py --help` for configuration and export options.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any
from datetime import datetime
import math

# IFC entity mappings in Fig. 2, with additional specific BER-relevant classes.
# Monitoring devices are mapped before energy equipment.
SEMANTIC_MAPPING = {
    "MonitoringDevice": ("IfcSensor", "IfcFlowMeter"),
    "SpatialUnit": ("IfcBuilding", "IfcBuildingStorey", "IfcSpace"),
    "EnvelopeComponent": ("IfcWall", "IfcWindow", "IfcDoor", "IfcSlab", "IfcRoof", "IfcCurtainWall"),
    "EnergySystem": ("IfcDistributionSystem",),
    "EnergyEquipment": ("IfcBoiler", "IfcChiller", "IfcPump", "IfcFan", "IfcLightFixture",
                        "IfcAirTerminal", "IfcUnitaryEquipment", "IfcCoil", "IfcHeatExchanger",
                        "IfcElectricAppliance"),
}
# EnergySource nodes are established from the building energy supply configuration.
# They are added through asset metadata in the operational data integration stage.
NODE_TYPES = set(SEMANTIC_MAPPING) | {"EnergySource"}
RELATION_SCHEMA = {
    "Bounds": {("EnvelopeComponent", "SpatialUnit")},
    "Contains": {("SpatialUnit", "SpatialUnit")},
    "Serves": {("EnergySystem", "SpatialUnit")},
    "Supplies": {("EnergySource", "EnergySystem")},
    "Includes": {("EnergySystem", "EnergyEquipment")},
    "Connects": {("EnergyEquipment", "EnergyEquipment")},
    "Monitors": {("MonitoringDevice", "SpatialUnit"), ("MonitoringDevice", "EnergyEquipment")},
}


def parse_timestamp(value: str) -> datetime:
    """Require an explicit time-zone offset; timestamps are compared as instants."""
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"Timestamp must include timezone: {value}")
    return t


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "wrappedValue"):
        return json_safe(value.wrappedValue)
    if hasattr(value, "is_a"):
        return {"ifc_type": value.is_a(), "id": getattr(value, "GlobalId", None),
                "name": getattr(value, "Name", None)}
    return str(value)


def read_rows(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        if Path(path).suffix.lower() == ".json":
            result = json.load(f)
            if not isinstance(result, list) or not all(isinstance(x, dict) for x in result):
                raise ValueError(f"Expected a JSON array of objects: {path}")
            return result
        return list(csv.DictReader(f))


class IFC2KGPipeline:
    def __init__(self, ifc_path: str | Path, output_dir: str | Path = "output"):
        self.ifc_path = Path(ifc_path)
        self.output_dir = Path(output_dir)
        self.ifc_file = None
        self.parsed_entities = []
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str, str], set[str]] = {}

    # Phase 1: schema-based entity parsing
    def phase_1_initialization_and_parsing(self):
        import ifcopenshell
        self.ifc_file = ifcopenshell.open(str(self.ifc_path))
        self.parsed_entities.clear()
        seen = set()
        for kind, classes in SEMANTIC_MAPPING.items():
            for cls in classes:
                for obj in self._by_type(cls):
                    gid = getattr(obj, "GlobalId", None)
                    if not gid or gid in seen:
                        continue
                    seen.add(gid)
                    self.parsed_entities.append((obj, kind))
        # IFC2x3 may represent equipment through generic occurrence classes
        # and specific assigned type objects (e.g., IfcPumpType).
        from ifcopenshell.util.element import get_type
        for obj in self._by_type("IfcDistributionElement"):
            gid = getattr(obj, "GlobalId", None)
            if not gid or gid in seen:
                continue
            assigned_type = get_type(obj)
            if assigned_type is None:
                continue
            for kind in ("MonitoringDevice", "EnergyEquipment"):
                if assigned_type.is_a() in {c + "Type" for c in SEMANTIC_MAPPING[kind]}:
                    seen.add(gid)
                    self.parsed_entities.append((obj, kind))
                    break
        # Project-specific IfcSystem groups or other unmapped assets may be
        # classified through approved metadata before relationship mapping.

    def _by_type(self, name):
        try:
            return self.ifc_file.by_type(name)
        except RuntimeError:
            # Some IFC2x3/IFC4 entities are version-specific.
            return []

    # Phase 2: IFC type, property sets, quantities, materials and units.
    # Original IFC units are retained; normalized aliases can be supplied in metadata.
    def phase_2_attribute_extraction(self):
        for obj, kind in self.parsed_entities:
            self._extract_node(obj, kind)

    def _extract_node(self, obj, kind):
        from ifcopenshell.util.element import get_psets, get_material, get_type
        from ifcopenshell.util.unit import get_project_unit
        psets = get_psets(obj, should_inherit=True)
        for pset in psets.values():
            if isinstance(pset, dict):
                pset.pop("id", None)
        assigned_type = get_type(obj)
        type_info = None if assigned_type is None else {
            "GlobalId": getattr(assigned_type, "GlobalId", None),
            "IfcType": assigned_type.is_a(),
            "Name": getattr(assigned_type, "Name", None),
            "PredefinedType": getattr(assigned_type, "PredefinedType", None),
        }
        mat = get_material(obj, should_inherit=True)
        material = self._material_details(mat)
        units = {}
        for unit_type in ("LENGTHUNIT", "AREAUNIT", "VOLUMEUNIT", "POWERUNIT",
                          "THERMODYNAMICTEMPERATUREUNIT", "ENERGYUNIT"):
            u = get_project_unit(self.ifc_file, unit_type)
            if u is not None:
                units[unit_type] = {"ifc_type": u.is_a(), "name": str(getattr(u, "Name", "")),
                                    "prefix": getattr(u, "Prefix", None), "ifc_id": u.id()}
        self.nodes[obj.GlobalId] = {
            "id": obj.GlobalId, "type": kind,
            "label": getattr(obj, "Name", None) or obj.GlobalId,
            "ifc_type": obj.is_a(),
            "properties": {"GlobalId": obj.GlobalId, "Name": getattr(obj, "Name", None),
                           "Description": getattr(obj, "Description", None),
                           "PredefinedType": getattr(obj, "PredefinedType", None),
                           "AssignedType": type_info,
                           "PropertySets": json_safe(psets), "Materials": material,
                           "ProjectUnits": units},
            "operational_records": [],
        }

    @staticmethod
    def _material_details(mat):
        if mat is None:
            return None
        # IfcMaterialLayerSetUsage/IfcMaterialLayerSet (walls and slabs).
        layer_set = getattr(mat, "ForLayerSet", mat)
        layers = getattr(layer_set, "MaterialLayers", None)
        if layers is not None:
            return {"ifc_type": mat.is_a(), "name": getattr(layer_set, "LayerSetName", None),
                    "layers": [
                        {"material": getattr(getattr(layer, "Material", None), "Name", None),
                         "thickness": getattr(layer, "LayerThickness", None)}
                        for layer in layers]}
        # IfcMaterialConstituentSet (e.g., door/window framing and glazing).
        constituents = getattr(mat, "MaterialConstituents", None)
        if constituents is not None:
            return {"ifc_type": mat.is_a(), "name": getattr(mat, "Name", None),
                    "constituents": [
                        {"name": getattr(c, "Name", None),
                         "material": getattr(getattr(c, "Material", None), "Name", None),
                         "fraction": getattr(c, "Fraction", None),
                         "category": getattr(c, "Category", None)}
                        for c in constituents]}
        # IFC2x3 material lists and IFC material profile sets.
        materials = getattr(mat, "Materials", None)
        if materials is not None:
            return {"ifc_type": mat.is_a(), "materials": [
                getattr(m, "Name", None) for m in materials]}
        profile_set = getattr(mat, "ForProfileSet", mat)
        profiles = getattr(profile_set, "MaterialProfiles", None)
        if profiles is not None:
            return {"ifc_type": mat.is_a(), "profiles": [
                {"name": getattr(p, "Name", None),
                 "material": getattr(getattr(p, "Material", None), "Name", None)}
                for p in profiles]}
        return {"ifc_type": mat.is_a(), "name": getattr(mat, "Name", None)}

    def _gid(self, obj):
        return getattr(obj, "GlobalId", None)

    def _add_edge(self, head, relation, tail, provenance, strict=False):
        if head not in self.nodes or tail not in self.nodes:
            if strict:
                raise ValueError(f"Unknown node in link: {head}, {relation}, {tail}")
            return
        pair = self.nodes[head]["type"], self.nodes[tail]["type"]
        if pair not in RELATION_SCHEMA.get(relation, set()) or head == tail:
            if strict:
                raise ValueError(f"Invalid schema relation: {pair}, {relation}")
            return
        self.edges.setdefault((head, relation, tail), set()).add(provenance)

    # Phase 3: semantic relationships grounded in native IFC associations
    def phase_3_topological_mapping(self):
        for rel in self._by_type("IfcRelSpaceBoundary"):
            self._add_edge(self._gid(rel.RelatedBuildingElement), "Bounds",
                           self._gid(rel.RelatingSpace), f"IFC:{rel.GlobalId}")
        for rel in self._by_type("IfcRelAggregates"):
            for part in rel.RelatedObjects:
                self._add_edge(self._gid(rel.RelatingObject), "Contains", self._gid(part),
                               f"IFC:{rel.GlobalId}")
        # Contains is restricted to SpatialUnit -> SpatialUnit in Fig. 2.
        for rel in self._by_type("IfcRelContainedInSpatialStructure"):
            for part in rel.RelatedElements:
                self._add_edge(self._gid(rel.RelatingStructure), "Contains", self._gid(part),
                               f"IFC:{rel.GlobalId}")
        for rel in self._by_type("IfcRelAssignsToGroup"):
            for obj in rel.RelatedObjects:
                self._add_edge(self._gid(rel.RelatingGroup), "Includes", self._gid(obj),
                               f"IFC:{rel.GlobalId}")
        for rel in self._by_type("IfcRelServicesBuildings"):
            for space in rel.RelatedBuildings:
                self._add_edge(self._gid(rel.RelatingSystem), "Serves", self._gid(space),
                               f"IFC:{rel.GlobalId}")
        for rel in self._by_type("IfcRelConnectsElements"):
            self._add_edge(self._gid(rel.RelatingElement), "Connects", self._gid(rel.RelatedElement),
                           f"IFC:{rel.GlobalId}")
        # Port connections: resolve native port owners; never infer service scope from proximity.
        port_owner = {}
        for rel in self._by_type("IfcRelConnectsPortToElement"):
            if rel.RelatingPort is not None:
                port_owner[rel.RelatingPort.id()] = self._gid(rel.RelatedElement)
        for rel in self._by_type("IfcRelNests"):
            for part in rel.RelatedObjects:
                if part.is_a("IfcPort"):
                    port_owner[part.id()] = self._gid(rel.RelatingObject)
        for rel in self._by_type("IfcRelConnectsPorts"):
            if rel.RelatingPort is None or rel.RelatedPort is None:
                continue
            self._add_edge(port_owner.get(rel.RelatingPort.id()), "Connects",
                           port_owner.get(rel.RelatedPort.id()), f"IFC:{rel.GlobalId}")

    # Phase 4: project metadata, supplemental links and operational records
    def integrate_asset_metadata(self, metadata=None):
        """Add energy supply information, confirmed asset classifications and attributes.

        Fields: id, type, label, properties (JSON object).
        Existing IFC GlobalIds identify existing building objects.
        """
        for row in read_rows(metadata):
            node_id, kind = row["id"], row["type"]
            if kind not in NODE_TYPES or not node_id:
                raise ValueError(f"Invalid node metadata: {node_id}, {kind}")
            props = row.get("properties", {})
            if isinstance(props, str):
                props = json.loads(props or "{}")
            if not isinstance(props, dict):
                raise ValueError("properties must be an object")
            if node_id in self.nodes and self.nodes[node_id]["type"] != kind:
                raise ValueError(f"Conflicting semantic type for {node_id}")
            # If metadata classifies a source IFC object (e.g. an IFC2x3
            # IfcSystem), retain its native attributes and GlobalId as well.
            if node_id not in self.nodes and self.ifc_file is not None:
                try:
                    obj = self.ifc_file.by_guid(node_id)
                except (RuntimeError, ValueError):
                    obj = None
                if obj is not None:
                    self._extract_node(obj, kind)
            node = self.nodes.setdefault(node_id, {"id": node_id, "type": kind, "label": node_id,
                                                    "ifc_type": "", "properties": {}, "operational_records": []})
            if row.get("label"):
                node["label"] = row["label"]
            # Original IFC payload is preserved, normalized aliases are separate.
            for k, v in props.items():
                if k in node["properties"] and node["properties"][k] != v:
                    raise ValueError(f"Metadata would overwrite source property: {node_id}/{k}")
                node["properties"][k] = v

    def integrate_operational_data(self, metadata=None, links=None, records=None):
        """Integrate the three project data inputs.

        metadata: id,type,label,properties (JSON object).
        links: source,relation,target,provenance.
        records: record_id,node_id,kind,indicator,value,unit,source_ref plus
        timestamp (ieq/equipment) or start,end,metering_scope (energy).
        """
        self.integrate_asset_metadata(metadata)
        # Re-evaluate native IFC links after any additional IFC assets have
        # been classified. Edge keys prevent duplicate relationships.
        if metadata and self.ifc_file is not None:
            self.phase_3_topological_mapping()
        for row in read_rows(links):
            if not row.get("provenance"):
                raise ValueError("Explicit links require provenance")
            self._add_edge(row["source"], row["relation"], row["target"], row["provenance"], strict=True)
        allowed_kind = {"ieq": "MonitoringDevice", "equipment": "EnergyEquipment", "energy": "EnergySource"}
        seen = {r["record_id"]: r for n in self.nodes.values() for r in n["operational_records"]}
        for row in read_rows(records):
            required = ("record_id", "node_id", "kind", "indicator", "value", "unit", "source_ref")
            if any(row.get(k) is None or row.get(k) == "" for k in required):
                raise ValueError(f"Missing mandatory operational field: {row}")
            node_id, kind = row["node_id"], row["kind"]
            if node_id not in self.nodes or allowed_kind.get(kind) != self.nodes[node_id]["type"]:
                raise ValueError(f"Incorrect operational record target: {node_id}, {kind}")
            clean = {k: row[k] for k in required}
            clean["value"] = float(clean["value"])
            if not math.isfinite(clean["value"]):
                raise ValueError("Nonfinite operational value")
            if kind == "energy":
                clean.update({k: row[k] for k in ("start", "end", "metering_scope")})
                if not clean["metering_scope"] or parse_timestamp(clean["start"]) >= parse_timestamp(clean["end"]):
                    raise ValueError("Invalid metering scope or interval")
                if clean["unit"] != "kWh" or clean["value"] < 0:
                    raise ValueError("Energy records require nonnegative interval consumption in kWh")
            else:
                clean["timestamp"] = row["timestamp"]
                parse_timestamp(clean["timestamp"])
            rid = clean["record_id"]
            if rid in seen:
                if seen[rid] != clean:
                    raise ValueError(f"Conflicting duplicate record: {rid}")
                continue
            seen[rid] = clean
            self.nodes[node_id]["operational_records"].append(clean)

    def export(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        edges = [{"source": h, "target": t, "type": r, "provenance": sorted(p)}
                 for (h, r, t), p in sorted(self.edges.items())]
        data = {"nodes": list(self.nodes.values()), "edges": edges}
        with (self.output_dir / "bi_kg.json").open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        # Neo4j-compatible tabular artifacts (nested attributes kept as JSON strings).
        with (self.output_dir / "bi_kg_nodes.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "type", "label", "ifc_type", "raw_properties", "operational_records"])
            writer.writeheader()
            for n in data["nodes"]:
                writer.writerow({**{k: n[k] for k in ("id", "type", "label", "ifc_type")},
                                 "raw_properties": json.dumps(n["properties"], ensure_ascii=False),
                                 "operational_records": json.dumps(n["operational_records"], ensure_ascii=False)})
        with (self.output_dir / "bi_kg_edges.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["source", "type", "target", "provenance"])
            writer.writeheader()
            for edge in edges:
                writer.writerow({**edge, "provenance": json.dumps(edge["provenance"], ensure_ascii=False)})
        print(f"BI-KG: {len(self.nodes)} nodes; {len(edges)} relationships. Review exported artifacts before import.")
        return data

    def visualize(self, data):
        from pyvis.network import Network
        colors = dict(zip(sorted(NODE_TYPES), ("#A4B96C", "#E6A23C", "#4C78A8", "#55A8A0", "#9E9AC8", "#E67C73")))
        net = Network(height="850px", width="100%", directed=True, notebook=False)
        for n in data["nodes"]:
            net.add_node(n["id"], label=n["label"], color=colors[n["type"]], title=n["type"])
        for e in data["edges"]:
            net.add_edge(e["source"], e["target"], title=e["type"], arrows="to")
        net.write_html(str(self.output_dir / "bi_kg_visualization.html"), notebook=False)


def import_reviewed_graph(path, uri, user, password, database="neo4j"):
    """Import an expert-reviewed artifact to a dedicated Neo4j database.

    Non-destructive MERGE; start with an empty graph for a clean experiment.
    No credentials are stored in this repository.
    """
    from neo4j import GraphDatabase
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = {n["id"]: n["type"] for n in data["nodes"]}
    if len(ids) != len(data["nodes"]) or not set(ids.values()) <= NODE_TYPES:
        raise ValueError("Invalid or duplicate BI-KG nodes")
    for e in data["edges"]:
        if e["source"] == e["target"] or (ids.get(e["source"]), ids.get(e["target"])) not in RELATION_SCHEMA.get(e["type"], set()):
            raise ValueError(f"Invalid edge: {e}")
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session(database=database) as session:
            session.run("CREATE CONSTRAINT bikg_id IF NOT EXISTS FOR (n:BIKGNode) REQUIRE n.id IS UNIQUE").consume()
            for kind in sorted(NODE_TYPES):
                rows = [{"id": n["id"], "type": n["type"], "label": n["label"],
                         "ifc_type": n["ifc_type"], "raw_properties": json.dumps(n["properties"], ensure_ascii=False),
                         "operational_records": json.dumps(n["operational_records"], ensure_ascii=False)}
                        for n in data["nodes"] if n["type"] == kind]
                session.run(f"UNWIND $rows AS row MERGE (n:BIKGNode {{id:row.id}}) SET n:{kind} SET n += row", rows=rows).consume()
            for relation in RELATION_SCHEMA:
                rows = [e for e in data["edges"] if e["type"] == relation]
                session.run(f"UNWIND $rows AS row MATCH (a:BIKGNode {{id:row.source}}), (b:BIKGNode {{id:row.target}}) MERGE (a)-[r:{relation}]->(b) SET r.provenance=row.provenance", rows=rows).consume()


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", nargs="?", help="Source IFC file")
    parser.add_argument("--metadata", help="Approved asset metadata (JSON/CSV)")
    parser.add_argument("--links", help="Approved supplemental relationships (JSON/CSV)")
    parser.add_argument("--records", help="Operational records (JSON/CSV)")
    parser.add_argument("--output", default="output")
    parser.add_argument("--visualize", action="store_true")
    parser.add_argument("--import-reviewed", metavar="JSON", help="Import a manually reviewed BI-KG artifact instead of extracting IFC")
    args = parser.parse_args()
    if args.import_reviewed:
        import_reviewed_graph(args.import_reviewed, os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"], os.getenv("NEO4J_DATABASE", "neo4j"))
        return
    if not args.ifc:
        parser.error("An IFC file or --import-reviewed is required")
    pipeline = IFC2KGPipeline(args.ifc, args.output)
    pipeline.phase_1_initialization_and_parsing()
    pipeline.phase_2_attribute_extraction()
    pipeline.phase_3_topological_mapping()
    pipeline.integrate_operational_data(args.metadata, args.links, args.records)
    data = pipeline.export()
    if args.visualize:
        pipeline.visualize(data)


if __name__ == "__main__":
    main()
