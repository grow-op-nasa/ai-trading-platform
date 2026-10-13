"""The Market Monitoring Agent loop -- src/ai/agents/monitoring/agent.py.

Reuses the Sprint 13 generic runtime unmodified (`LLMProvider`,
`ToolRegistry`, `ToolResult`, `ToolCallRecord`); only the bookkeeping,
budgets and report shape are monitoring-specific (same arrangement as
the Strategy Development Agent, ADR-0047/ADR-0050).

Budgets (`max_steps`, `max_monitoring_runs`) are enforced here in code.
Exceeding a budget ends the run as BUDGET_EXHAUSTED with a partial
report that says so. Deterministic limitations (not-evaluable checks,
unknown run status, uncited/unknown ids) are appended by code, not left
to the model.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from src.ai.agents.models import ToolCallRecord, utc_now
from src.ai.agents.monitoring.models import (
    STATUS_BUDGET_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    MonitoringAgentReport,
    MonitoringAgentRun,
)
from src.ai.agents.monitoring.policy import MarketMonitoringAgentPolicy
from src.ai.agents.monitoring.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from src.ai.agents.monitoring.session import MonitoringAgentSession
from src.ai.agents.monitoring.store import MonitoringAgentRunStore
from src.ai.agents.provider import LLMMessage, LLMProvider, LLMToolCall, ProviderError
from src.ai.agents.tools import ToolRegistry, ToolResult

_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
RUN_TOOL = "run_monitoring_checks"
RUN_RESULT_TOOLS = (RUN_TOOL, "get_monitoring_run", "get_market_data_health")
_ABBREVIATED_ID = re.compile(r"\b([0-9a-f]{6,31})(?:\.\.\.|\u2026)")
_HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")
_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def _safe_message(message: str | None) -> str | None:
    """Tool error text is bounded and credential-patterns are redacted before it is audited or shown."""
    if message is None:
        return None
    return _SECRET.sub("[REDACTED]", message)[:300]
_NOT_EXECUTED = {"POLICY_REJECTED", "UNKNOWN_TOOL", "INVALID_ARGUMENTS", _BUDGET_EXHAUSTED}


class MarketMonitoringAgent:
    """Orchestrates one bounded monitoring investigation."""

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        policy: MarketMonitoringAgentPolicy | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        system_prompt_version: str = SYSTEM_PROMPT_VERSION,
        run_store: MonitoringAgentRunStore | None = None,
        persist: bool = True,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._policy = policy or MarketMonitoringAgentPolicy()
        self._system_prompt = system_prompt
        self._prompt_version = system_prompt_version
        self._run_store = run_store or MonitoringAgentRunStore()
        self._persist = persist

    def run(self, goal: str) -> MonitoringAgentRun:
        run_id = uuid.uuid4().hex
        started_at = utc_now()
        session = MonitoringAgentSession(goal=goal)
        session.add_message(LLMMessage(role="user", content=goal))
        tool_defs = self._tools.allowed_definitions(self._policy)
        status, error, report = STATUS_COMPLETED, None, None
        try:
            report = self._loop(run_id, goal, session, tool_defs)
        except ProviderError as exc:
            status, error = STATUS_FAILED, f"{exc.code}: {exc.message}"
        except _BudgetExhausted as exc:
            status = STATUS_BUDGET_EXHAUSTED
            report = self._build_report(
                run_id, goal, status,
                "The run's budget was exhausted before a final answer could be produced.",
                session,
                [f"Budget exhausted ({exc.reason}); anything above is incomplete, not a finished assessment."],
            )
        run = MonitoringAgentRun(
            run_id=run_id, goal=goal, provider=self._provider.name, model=self._provider.model,
            status=status, started_at=started_at, finished_at=utc_now(), step_count=session.step_count,
            tool_call_count=len(session.tool_calls), monitoring_run_count=session.monitoring_run_count,
            system_prompt_version=self._prompt_version,
            tool_schema_version=self._tools.schema_hash(self._policy),
            policy_hash=self._policy.config_hash(), tool_calls=list(session.tool_calls),
            monitoring_run_ids=list(session.monitoring_run_ids), final_report=report, error=error,
        )
        if self._persist:
            self._run_store.save(run)
        return run

    # ------------------------------------------------------------------
    def _loop(self, run_id, goal, session: MonitoringAgentSession, tool_defs) -> MonitoringAgentReport:
        while True:
            if session.step_count >= self._policy.max_steps:
                raise _BudgetExhausted(f"max_steps={self._policy.max_steps}")
            session.step_count += 1
            response = self._provider.generate(
                system=self._system_prompt, messages=session.messages, tools=tool_defs
            )
            if not response.tool_calls:
                text = response.text or ""
                session.add_message(LLMMessage(role="assistant", content=text))
                extra = []
                if getattr(response, "stop_reason", "end_turn") == "max_tokens":
                    # Code-derived: the model's report was cut off by the output-token cap, so
                    # the final item(s) may be incomplete. The model cannot omit this.
                    extra.append("The model's final report was truncated by the output-token limit; "
                                 "the last item(s) may be incomplete.")
                return self._build_report(run_id, goal, STATUS_COMPLETED, text, session, extra)
            session.add_message(
                LLMMessage(role="assistant", content=response.text or "", tool_calls=response.tool_calls)
            )
            for call in response.tool_calls:
                result = self._handle_tool_call(call, session)
                session.add_message(
                    LLMMessage(role="tool", content=json.dumps(_payload(result), default=str),
                               tool_call_id=call.id)
                )
                if result.error_code == _BUDGET_EXHAUSTED:
                    raise _BudgetExhausted(f"max_monitoring_runs={self._policy.max_monitoring_runs}")
            # ADR-0051: the runtime, not the model's willingness, guarantees that every finding of a
            # retrieved monitoring run gets its full detail fetched through the registered tool.
            self._hydrate_required_findings(session)

    # ------------------------------------------------------------------ mandatory evidence retrieval
    @staticmethod
    def _required_finding_ids(session: MonitoringAgentSession) -> list[str]:
        """Authoritative ids: those in successful monitoring-run results (run / stored run / market
        data health), as returned by the platform's own tools -- never ids the model wrote."""
        out: list[str] = []
        for rec in session.tool_calls:
            if rec.tool_name not in RUN_RESULT_TOOLS or rec.is_error:
                continue
            findings = rec.result_summary.get("findings")
            if not isinstance(findings, list):
                continue
            for f in findings:
                fid = f.get("finding_id") if isinstance(f, dict) else None
                if isinstance(fid, str) and _HEX_ID.fullmatch(fid) and fid not in out:
                    out.append(fid)
        return out

    def _hydrate_required_findings(self, session: MonitoringAgentSession) -> None:
        required = self._required_finding_ids(session)
        todo = [i for i in required if i not in session.retrieved_finding_ids
                and i not in session.runtime_attempted]
        if not todo:
            return
        room = max(self._policy.max_mandatory_finding_retrievals - len(session.runtime_attempted), 0)
        batch, over = todo[:room], todo[room:]
        for fid in over:
            session.retrieval_failures.setdefault(fid, "RETRIEVAL_LIMIT")
        if not batch:
            return
        calls = [LLMToolCall(id=f"runtime-{uuid.uuid4().hex[:12]}", name="get_finding",
                             arguments={"finding_id": fid}) for fid in batch]
        session.add_message(LLMMessage(
            role="assistant",
            content=(f"[runtime] Mandatory evidence retrieval: fetching the full detail of {len(batch)} "
                     "finding(s) from the retrieved monitoring run via get_finding."),
            tool_calls=calls,
        ))
        for call in calls:
            session.runtime_attempted.add(call.arguments["finding_id"])
            result = self._handle_tool_call(call, session, initiated_by="runtime")
            session.add_message(
                LLMMessage(role="tool", content=json.dumps(_payload(result), default=str), tool_call_id=call.id)
            )

    @staticmethod
    def _note_finding_detail(call: LLMToolCall, result: ToolResult, session: MonitoringAgentSession) -> bool:
        """True only when the registered handler returned the requested finding's full detail."""
        fid = call.arguments.get("finding_id") if isinstance(call.arguments, dict) else None
        if not isinstance(fid, str):
            return False
        if result.is_error:
            session.retrieval_failures[fid] = result.error_code or "ERROR"
            return False
        data = result.data
        if not isinstance(data, dict) or not isinstance(data.get("finding_id"), str):
            session.retrieval_failures[fid] = "MALFORMED_RESULT"
            return False
        if data["finding_id"] != fid:
            session.retrieval_failures[fid] = "ID_MISMATCH"
            return False
        if not isinstance(data.get("evidence"), list):
            session.retrieval_failures[fid] = "MALFORMED_RESULT"
            return False
        if fid not in session.retrieved_finding_ids:
            session.retrieved_finding_ids.append(fid)
        session.retrieval_failures.pop(fid, None)
        return True

    def _handle_tool_call(self, call: LLMToolCall, session: MonitoringAgentSession,
                          initiated_by: str = "model") -> ToolResult:
        if call.name == RUN_TOOL and session.monitoring_run_count >= self._policy.max_monitoring_runs:
            result = ToolResult.error(
                call.name, _BUDGET_EXHAUSTED,
                f"max_monitoring_runs ({self._policy.max_monitoring_runs}) already reached",
            )
        else:
            result = self._tools.execute(call.name, call.arguments, self._policy)
        if call.name == RUN_TOOL and result.error_code not in _NOT_EXECUTED:
            session.monitoring_run_count += 1  # an attempted execution spends budget
        session.record_tool_call(
            ToolCallRecord(
                step=session.step_count, tool_name=call.name,
                arguments=dict(call.arguments) if isinstance(call.arguments, dict) else {},
                is_error=result.is_error, result_summary=result.data if not result.is_error else {},
                error_code=result.error_code, error_message=_safe_message(result.error_message),
                initiated_by=initiated_by,
            )
        )
        valid_detail = self._note_finding_detail(call, result, session) if call.name == "get_finding" else True
        if not result.is_error and valid_detail:
            self._absorb(call.name, result.data, session)
        return result

    @staticmethod
    def _absorb(name: str, data: dict[str, Any], session: MonitoringAgentSession) -> None:
        run_id = data.get("run_id")
        if name in (RUN_TOOL, "get_monitoring_run", "get_market_data_health") and run_id:
            session.note_run(run_id)
        findings = data.get("findings")
        if isinstance(findings, list):
            ids = [f.get("finding_id") for f in findings if isinstance(f, dict)]
            session.note_findings(ids)
            session.note_evidence(ids)
        if name == "get_finding" and data.get("finding_id"):
            session.note_findings([data["finding_id"]])
            session.note_evidence([data["finding_id"]] + [e.get("reference") for e in data.get("evidence", [])])
        if run_id:
            session.note_evidence([run_id])

    def _build_report(self, run_id, goal, status, text, session, limitations=None) -> MonitoringAgentReport:
        facts, hyps, sugg, lims = _parse_sections(text)
        lims = list(lims) + list(limitations or []) + self._derived_limitations(facts, session)
        required = tuple(self._required_finding_ids(session))
        retrieved = tuple(i for i in required if i in session.retrieved_finding_ids)
        incomplete = tuple(i for i in required if i not in retrieved)
        reasons = tuple(f"{i}: {session.retrieval_failures.get(i, 'NOT_RETRIEVED')}" for i in incomplete)
        run_seen = any(r.tool_name in RUN_RESULT_TOOLS and not r.is_error for r in session.tool_calls)
        if incomplete:
            lims.append(
                f"INCOMPLETE EVIDENCE: full detail (via get_finding) was NOT retrieved for {len(incomplete)} of "
                f"{len(required)} finding(s) of the monitoring run ({'; '.join(reasons)}); statements about "
                "them rest on summary fields only."
            )
        elif not run_seen:
            lims.append("INCOMPLETE EVIDENCE: no monitoring run's results were retrieved in this agent run.")
        return MonitoringAgentReport(
            run_id=run_id, goal=goal, status=status, summary=text, observed_facts=tuple(facts),
            hypotheses=tuple(hyps), suggested_investigations=tuple(sugg),
            limitations=tuple(dict.fromkeys(lims)), monitoring_run_ids=tuple(session.monitoring_run_ids),
            finding_ids=tuple(session.finding_ids), evidence_references=tuple(session.evidence_references),
            evidence_complete=bool(run_seen and not incomplete), findings_required=required,
            findings_retrieved=retrieved, findings_incomplete=incomplete, evidence_incomplete_reasons=reasons,
        )

    @staticmethod
    def _derived_limitations(facts: list[str], session: MonitoringAgentSession) -> list[str]:
        out: list[str] = []
        if not session.tool_calls:
            out.append("No monitoring tool was called: no statement in this report is backed by tool evidence.")
        seen = set(session.evidence_references) | set(session.monitoring_run_ids) | set(session.finding_ids)
        # ids in tool results the model saw (any hex token in recorded results)
        for rec in session.tool_calls:
            seen |= set(_HEX_ID.findall(json.dumps(rec.result_summary, default=str)))
        uncited = {i for f in facts for i in _HEX_ID.findall(f)} - seen
        if uncited:
            out.append(
                "Observed statements cite ids no tool returned in this run "
                f"({', '.join(sorted(uncited))}); treat those statements as unverified."
            )
        for rec in session.tool_calls:
            # every tool that returns a monitoring run's check results is covered, not only a fresh run:
            # an agent inspecting a stored UNKNOWN run must still carry the limitation.
            if rec.tool_name in RUN_RESULT_TOOLS and not rec.is_error:
                st = rec.result_summary.get("status")
                if rec.tool_name != "get_market_data_health" and st == "UNKNOWN":
                    out.append(
                        f"Monitoring run {rec.result_summary.get('run_id')} is UNKNOWN: at least one "
                        "check could not be evaluated, so the platform is not confirmed healthy."
                    )
                for c in rec.result_summary.get("checks", []):
                    if c.get("status") == "NOT_EVALUABLE":
                        out.append(f"Check {c.get('check_id')} was not evaluable: {c.get('reason')}")
        abbreviated = sorted({m for f in facts for m in _ABBREVIATED_ID.findall(f)})
        if abbreviated:
            out.append(
                "Observed statements abbreviate ids (" + ", ".join(abbreviated) + "...); the full ids are "
                "in this report's finding_ids / monitoring_run_ids fields."
            )
        required = set(MarketMonitoringAgent._required_finding_ids(session))
        unfetched = sorted(set(session.finding_ids) - set(session.retrieved_finding_ids) - required)
        if unfetched:
            out.append(
                f"Full evidence was not fetched (get_finding not called) for {len(unfetched)} finding(s): "
                f"{', '.join(unfetched)}; their details are summary fields from other tools."
            )
        return out


class _BudgetExhausted(Exception):
    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


def _payload(result: ToolResult) -> dict[str, Any]:
    if result.is_error:
        return {"status": "error", "error_code": result.error_code, "error_message": _safe_message(result.error_message)}
    return {"status": "ok", "data": result.data}


_HEADER = re.compile(
    r"^(?:[-*]\s*)?(?P<name>observed(?: facts?)?|hypothes[ei]s|suggested investigations?|limitations?)"
    r"\b[^:]*:\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_KEYS = {"observed": "facts", "hypothes": "hyps", "suggested": "sugg", "limitation": "lims"}
_BULLET = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s+(?P<body>.+)$")


def _parse_sections(text: str):
    """Extract labelled sections from the model's final text. Supports both
    `Observed: item` on one line and a bare `Observed:` header followed by
    bullet lines (what a real model produced in the first live run). Empty
    items are never recorded."""
    buckets: dict[str, list[str]] = {"facts": [], "hyps": [], "sugg": [], "lims": []}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADER.match(line)
        if m:
            name = m.group("name").lower()
            current = next(v for k, v in _KEYS.items() if name.startswith(k))
            rest = m.group("rest").strip()
            if rest:
                buckets[current].append(rest)
            continue
        if line.lower().startswith("summary"):
            current = None
            continue
        b = _BULLET.match(line)
        if b and current:
            buckets[current].append(b.group("body").strip())
        elif not b:
            current = None  # free prose ends the list
    return buckets["facts"], buckets["hyps"], buckets["sugg"], buckets["lims"]
