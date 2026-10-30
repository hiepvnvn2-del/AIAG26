"""Shared Pydantic schemas for the RPA 2.0 DAG."""
from __future__ import annotations
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

# --- Structured Error Types ---
class ScrapeErrorCode(str, Enum):
    TIMEOUT = "TIMEOUT"
    BLOCKED_403 = "BLOCKED_403"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SSRF_DETECTED = "SSRF_DETECTED"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    NO_PRICES_FOUND = "NO_PRICES_FOUND"
    POPUP_UNRESOLVED = "POPUP_UNRESOLVED"
    LLM_EXTRACTION_FAILED = "LLM_EXTRACTION_FAILED"
    UNKNOWN = "UNKNOWN"


class ScrapeError(BaseModel):
    error_code: ScrapeErrorCode
    url: str
    message: str
    step_occurred: str = "navigation"


# --- Orchestrator: DAG output ---
class WorkerTask(BaseModel):
    worker_id: str
    target_name: str
    start_url: str
    goal: str
    target_fields: list[str] = Field(default_factory=lambda: ["product_name", "price_vnd", "billing_cycle", "stock_status"])


class BusinessRuleEval(BaseModel):
    condition_description: str = "If any IN_STOCK competitor is cheaper than us by >= 10%"
    pricing_formula: str = "proposed_price = min_competitor_price * 0.98"


class MutationActionTask(BaseModel):
    enabled: bool = True
    target_system: str = "Internal Shop Admin Portal"
    action_description: str
    requires_hitl_approval: bool = True
    hitl_channel: Literal["telegram", "webhook", "email"] = "telegram"


class WorkflowDAG(BaseModel):
    workflow_id: str
    intent_summary: str
    parallel_worker_tasks: list[WorkerTask]
    business_rule_evaluation: BusinessRuleEval
    mutation_action_task: MutationActionTask

# --- Browser worker ReAct turn ---
class AgentTurn(BaseModel):
    thought: str
    self_healing_triggered: bool = False
    action_type: Literal["CLICK", "TYPE", "PRESS_KEY", "SCROLL_DOWN", "WAIT", "EXTRACT_DATA", "REQUEST_HITL_APPROVAL", "FAIL"] = "WAIT"
    target_element_id: Optional[int] = None
    input_text: Optional[str] = None
    extracted_payload: list[dict] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_turn_data(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize action field alias
            if "action" in data and "action_type" not in data:
                data["action_type"] = data["action"]
            if "value" in data and "input_text" not in data:
                data["input_text"] = data["value"]
            if "action_type" in data and isinstance(data["action_type"], str):
                act = data["action_type"].upper().strip()
                valid_acts = {"CLICK", "TYPE", "PRESS_KEY", "SCROLL_DOWN", "WAIT", "EXTRACT_DATA", "REQUEST_HITL_APPROVAL", "FAIL"}
                if act in valid_acts:
                    data["action_type"] = act
                elif act in ("EXTRACT", "EXTRACT_PRODUCTS", "EXTRACT_TABLE"):
                    data["action_type"] = "EXTRACT_DATA"
                elif act in ("APPROVAL", "HITL", "REQUEST_APPROVAL", "REQUEST_HITL"):
                    data["action_type"] = "REQUEST_HITL_APPROVAL"
                else:
                    data["action_type"] = "WAIT"
        return data

# --- Normalizer ---
class CompetitorEntry(BaseModel):
    source: str
    normalized_price_vnd: int
    stock_status: Literal["IN_STOCK", "OUT_OF_STOCK", "LIMITED"]
    diff_percentage: float


class ComparisonRow(BaseModel):
    product_code: str
    product_name: str
    our_current_price_vnd: int
    competitors: list[CompetitorEntry] = Field(default_factory=list)
    action_required: bool = False
    recommended_new_price_vnd: Optional[int] = None
    reason_vn: str


class NormalizerOutput(BaseModel):
    analysis_summary_vn: str
    comparison_table: list[ComparisonRow]

# --- HITL ---
class ApprovalRequest(BaseModel):
    workflow_id: str
    risk_level: Literal["MEDIUM", "HIGH", "CRITICAL"]
    before_values: dict
    after_values: dict
    impact_vn: str
    staged_action: str
    # optional screenshot b64 preview — not validated
    screenshot_b64: Optional[str] = None
    model_config = {"extra": "allow"}

class HITLCallback(BaseModel):
    workflow_id: str
    decision: Literal["APPROVE", "REJECT"]
    by: str = "telegram"


# --- Generic Competitor URL Analysis Models ---
class CompetitorProduct(BaseModel):
    name: str
    price_vnd: Optional[int] = None
    original_price_vnd: Optional[int] = None
    raw_price_text: str = ""
    billing_cycle: Literal["month", "year", "one_time", "unknown"] = "month"
    stock: Literal["IN_STOCK", "LIMITED", "OUT_OF_STOCK", "UNKNOWN"] = "IN_STOCK"
    specs: dict = Field(default_factory=dict)
    source_url: str = ""
    category: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_type: Literal["json_ld", "dom", "firecrawl", "unknown"] = "unknown"

    @model_validator(mode="before")
    @classmethod
    def pre_normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Map alias keys
            if "name" not in data and "product_name" in data:
                data["name"] = data["product_name"]
            elif "name" not in data and "title" in data:
                data["name"] = data["title"]

            if "raw_price_text" not in data and "price_raw" in data:
                data["raw_price_text"] = str(data["price_raw"] or "")
            elif "raw_price_text" not in data and "price" in data:
                data["raw_price_text"] = str(data["price"] or "")

            # Auto-extract price_vnd if missing
            if not data.get("price_vnd") and data.get("raw_price_text"):
                from app.services.normalizer import normalize_price_and_cycle
                pvnd, pcycle, _ = normalize_price_and_cycle(data["raw_price_text"])
                if pvnd:
                    data["price_vnd"] = pvnd
                if pcycle != "unknown" and not data.get("billing_cycle"):
                    data["billing_cycle"] = pcycle

            if "original_price_raw" in data and not data.get("original_price_vnd"):
                from app.services.normalizer import normalize_price_and_cycle
                opvnd, _, _ = normalize_price_and_cycle(str(data["original_price_raw"] or ""))
                if opvnd:
                    data["original_price_vnd"] = opvnd

        return data

    @field_validator("billing_cycle", mode="before")
    @classmethod
    def normalize_cycle(cls, v):
        s = str(v or "").lower().strip()
        if s in ("year", "yearly", "annual", "năm", "12 tháng"):
            return "year"
        if s in ("month", "monthly", "tháng"):
            return "month"
        if s in ("one_time", "onetime", "one-time", "lifetime", "trọn đời"):
            return "one_time"
        return "unknown"

    @field_validator("stock", mode="before")
    @classmethod
    def normalize_stock_field(cls, v):
        s = str(v or "").upper().strip()
        if "IN" in s or "CÒN" in s or "AVAILABLE" in s:
            return "IN_STOCK"
        if "OUT" in s or "HẾT" in s:
            return "OUT_OF_STOCK"
        if "LIMIT" in s or "SẮP" in s:
            return "LIMITED"
        return "UNKNOWN"

    @field_validator("specs", mode="before")
    @classmethod
    def sanitize_specs(cls, v):
        if not isinstance(v, dict):
            return {}
        return v



class MatchedItem(BaseModel):
    our_sku: str
    competitor: str
    competitor_product: CompetitorProduct
    match_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    delta_pct: float
    note: str = ""


class PriceAnalysisReport(BaseModel):
    matches: list[MatchedItem] = Field(default_factory=list)
    market_min: Optional[int] = None
    market_median: Optional[int] = None
    our_position: Literal["CHEAPER", "PARITY", "PRICIER"] = "PARITY"
    suggestions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    failed_urls: dict[str, str] = Field(default_factory=dict)


class AnalyzeURLRequest(BaseModel):
    urls: list[str]


# --- Natural Language Intent Router Models ---
class IntentResult(BaseModel):
    intent: Literal[
        "SHOW_CATALOG",
        "RUN_COMPETITOR_SCAN",
        "ANALYZE_URL",
        "QUERY_COMPETITOR_CACHE",
        "PRICE_ADVISORY",
        "APPLY_PRICE_CHANGE",
        "APPROVE",
        "REJECT",
        "GENERAL_CHAT",
        "UNCLEAR",
    ]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    entities: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False
    clarifying_question: Optional[str] = None

