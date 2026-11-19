# Generative Decision Support for Building Energy Retrofit (BER)

This repository contains the core implementation code and prompt templates for the framework described in the study **“Generative decision support for building energy retrofit: An LLM-driven multi-agent framework integrating building and regulatory knowledge graphs.”** The framework integrates a building information knowledge graph (BI-KG) and a regulatory code knowledge graph (RC-KG) with four specialized LLM-driven agents to support BER decision-making.

## Repository contents

### 1. [01_ifc2kg_pipeline.py](01_ifc2kg_pipeline.py)

Implements the IFC2KG pipeline for BI-KG construction, including IFC entity parsing, attribute extraction, semantic relationship mapping, and operational data integration. The resulting graph can be exported and imported into Neo4j.

### 2. [02_prompts_regulatory_kg.md](02_prompts_regulatory_kg.md)

Provides prompt templates for LLM-driven regulatory knowledge triple generation through three stages: initial extraction, iterative feedback, and final canonicalization.

### 3. [03_building_graph_retrieval_tools.py](03_building_graph_retrieval_tools.py)

Implements six predefined BI-KG retrieval functions for building attributes, spatial elements, semantic relationships, indoor environmental quality records, equipment operating logs, and energy consumption. The functions support task-specific data filtering and aggregation through Function Calling.

### 4. [04_prompts_mas_agents.md](04_prompts_mas_agents.md)

Provides system prompt templates for the Task Manager, Building Analyst, Regulation Specialist, and Retrofit Consultant, defining their responsibilities, CoT or ReAct reasoning workflows, and structured input and output formats.

### 5. [05_mas_orchestration_engine.py](05_mas_orchestration_engine.py)

Implements dynamic task routing, agent coordination, ReAct-based knowledge retrieval, and evidence synthesis. The engine supports parallel dual-track retrieval, graph-enhanced RC-KG retrieval using vector search and relationship traversal, and computational cost measurement (RT, AC, and TC).

The repository provides the core implementation code and prompt templates of the proposed BER decision support framework. Execution requires prepared knowledge graphs, regulatory embeddings, and LLM API credentials.
