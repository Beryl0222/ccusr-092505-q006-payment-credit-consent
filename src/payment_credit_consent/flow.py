"""支付与信贷确认流程服务。

命令侧入口，全部业务规则在此落地：

- 支付可以在没有信贷时完成；信贷拒绝只影响报价，不改变订单；
- 同一 command_id 重试返回原决定，载荷不一致报 idempotency_conflict；
- 逐项确认绑定同意纪元 (报价版本, 风险等级)，报价或风险等级变化后
  必须重新逐项确认，不能沿用旧同意；
- 适当性评估出现异常进入人工复核，复核前不得综合确认；
- 撤回与提前结清只影响允许的后续动作，历史账单与收款主体仍可审计；
- 状态全部由 events.jsonl 折叠重建，服务重启后可继续未完成的确认、
  人工复核与到期提醒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from . import events as ev
from .contracts import validate_event
from .domain import (
    CONTRACT_ACTIVE,
    CONTRACT_REVOKED,
    ORDER_OPENED_STATUS,
    ORDER_PAID,
    REQUIRED_CONSENT_ITEMS,
    SCHEDULE_SCHEDULED,
    SUITABILITY_APPROVED,
    SUITABILITY_AUTO_CLEAR,
    SUITABILITY_DECLINED,
    SUITABILITY_PENDING_REVIEW,
    CheckoutState,
    DisputeState,
    parse_ts,
)
from .ledger import CommandLog, EventStore, request_hash

EVENTS_FILE = "events.jsonl"
COMMANDS_FILE = "commands.jsonl"


class ContractViolationError(Exception):
    """服务内部构造的事件不符合交换契约，属于实现缺陷。"""


@dataclass(frozen=True)
class Decision:
    """命令决定：成功时携带追加的事件，失败时携带原因与细节。"""

    command_id: str
    ok: bool
    reason: str | None
    events: tuple[dict[str, Any], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


class ConsentFlow:
    """支付与信贷同意账本的命令服务，可直接从持久化目录恢复。"""

    def __init__(self, store_dir: Path | str, schema: Mapping[str, Any]):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._schema = schema
        self._store = EventStore(self._dir / EVENTS_FILE)
        self._commands = CommandLog(self._dir / COMMANDS_FILE)
        self._orders: dict[str, CheckoutState] = {}
        self._disputes: dict[str, DisputeState] = {}
        self._order_events: dict[str, list[str]] = {}
        self._command_events: dict[str, list[str]] = self._store.command_events()
        for event in self._store.all():
            self._route(event)

    @classmethod
    def restart(cls, store_dir: Path | str, schema: Mapping[str, Any]) -> "ConsentFlow":
        """服务重启：从账本目录恢复全部状态，继续未完成的确认与提醒。"""
        return cls(store_dir, schema)

    # ------------------------------------------------------------------
    # 命令执行骨架：幂等 + 契约校验 + 状态折叠
    # ------------------------------------------------------------------
    def _execute(
        self,
        command_id: str,
        request: dict[str, Any],
        handler: Callable[[], "Decision"],
    ) -> Decision:
        digest = request_hash(request)
        record = self._commands.get(command_id)
        event_ids = self._command_events.get(command_id)
        if event_ids:
            if record and record["request_hash"] != digest:
                return Decision(command_id, False, "idempotency_conflict")
            events = tuple(self._store.get(event_id) for event_id in event_ids)
            return Decision(command_id, True, None, events)
        if record is not None:
            if record["request_hash"] != digest:
                return Decision(command_id, False, "idempotency_conflict")
            return Decision(command_id, record["ok"], record["reason"], (), record.get("details", {}))
        try:
            parse_ts(request["occurred_at"])
        except (KeyError, TypeError, ValueError):
            decision = Decision(command_id, False, "invalid_occurred_at")
            self._commands.record(command_id, digest, False, decision.reason, [])
            return decision
        decision = handler()
        self._commands.record(
            command_id,
            digest,
            decision.ok,
            decision.reason,
            [event["event_id"] for event in decision.events],
            decision.details,
        )
        if decision.ok and decision.events:
            self._command_events[command_id] = [event["event_id"] for event in decision.events]
        return decision

    def _append(
        self,
        out: list[dict[str, Any]],
        command_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        summary: str,
        facts: dict[str, Any],
    ) -> None:
        event = ev.build_event(
            event_type,
            aggregate_type,
            aggregate_id,
            occurred_at,
            self._store.next_version(aggregate_type, aggregate_id),
            summary,
            facts,
            command_id,
        )
        issues = validate_event(event, self._schema)
        if issues:
            raise ContractViolationError(
                "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
            )
        stored = self._store.append(event)
        self._route(stored)
        out.append(stored)

    def _route(self, event: dict[str, Any]) -> None:
        if event["aggregate_type"] == ev.AGGREGATE_DISPUTE_CASE:
            state = self._disputes.setdefault(
                event["aggregate_id"], DisputeState(event["aggregate_id"])
            )
            state.apply(event)
            order_id = event["facts"].get("order_id")
            if order_id:
                self._order_events.setdefault(order_id, []).append(event["event_id"])
            return
        order_id = self._order_key(event)
        if order_id is None:
            return
        state = self._orders.setdefault(order_id, CheckoutState(order_id))
        state.apply(event)
        self._order_events.setdefault(order_id, []).append(event["event_id"])

    @staticmethod
    def _order_key(event: dict[str, Any]) -> str | None:
        if event["aggregate_type"] == ev.AGGREGATE_PAYMENT_ORDER:
            return event["aggregate_id"]
        return event.get("facts", {}).get("order_id")

    @staticmethod
    def _reject(command_id: str, reason: str, details: dict[str, Any] | None = None) -> Decision:
        return Decision(command_id, False, reason, (), details or {})

    def _opened_order(self, command_id: str, order_id: str) -> CheckoutState | Decision:
        state = self._orders.get(order_id)
        if state is None or state.order_status == "unknown":
            return self._reject(command_id, "order_not_found")
        if state.order_status != ORDER_OPENED_STATUS:
            return self._reject(command_id, "already_confirmed", {"order_status": state.order_status})
        return state

    # ------------------------------------------------------------------
    # 下单与报价
    # ------------------------------------------------------------------
    def open_checkout(
        self,
        command_id: str,
        *,
        order_id: str,
        customer_id: str,
        items: list[dict[str, Any]],
        total_minor: int,
        discount_minor: int,
        currency: str,
        merchant_party_id: str,
        platform_party_id: str,
        occurred_at: str,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            if order_id in self._orders:
                return self._reject(command_id, "order_exists")
            payable = total_minor - discount_minor
            if payable < 0 or total_minor < 0 or discount_minor < 0:
                return self._reject(command_id, "amount_mismatch")
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.ORDER_OPENED, ev.AGGREGATE_PAYMENT_ORDER, order_id,
                occurred_at, f"订单 {order_id} 已创建，待用户确认支付",
                {
                    "order_id": order_id,
                    "customer_id": customer_id,
                    "items": items,
                    "total_minor": total_minor,
                    "discount_minor": discount_minor,
                    "payable_minor": payable,
                    "currency": currency,
                    "merchant_party_id": merchant_party_id,
                    "platform_party_id": platform_party_id,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def _validate_offer_facts(
        self,
        command_id: str,
        state: CheckoutState,
        terms: dict[str, Any],
        parties: list[dict[str, Any]],
        collection_party_id: str,
        repayment_schedule: list[dict[str, Any]],
        display_copy: dict[str, Any],
    ) -> Decision | None:
        term_keys = {
            "principal_minor", "apr_bp", "installments",
            "per_installment_minor", "total_interest_minor", "total_cost_minor",
        }
        if not term_keys.issubset(terms):
            return self._reject(command_id, "terms_incomplete", {"missing": sorted(term_keys - set(terms))})
        if terms["principal_minor"] != state.payable_minor:
            return self._reject(
                command_id, "principal_mismatch",
                {"principal_minor": terms["principal_minor"], "payable_minor": state.payable_minor},
            )
        if sum(entry["amount_minor"] for entry in repayment_schedule) != terms["total_cost_minor"]:
            return self._reject(command_id, "schedule_mismatch")
        party_ids = {party["party_id"] for party in parties}
        roles = {party["role"] for party in parties}
        if "lender" not in roles or collection_party_id not in party_ids:
            return self._reject(command_id, "party_incomplete")
        block_keys = {block["key"] for block in display_copy.get("blocks", [])}
        missing_blocks = sorted(set(REQUIRED_CONSENT_ITEMS) - block_keys)
        if missing_blocks:
            return self._reject(command_id, "display_incomplete", {"missing_blocks": missing_blocks})
        return None

    def show_credit_offer(
        self,
        command_id: str,
        *,
        order_id: str,
        offer_id: str,
        terms: dict[str, Any],
        fees: list[dict[str, Any]],
        parties: list[dict[str, Any]],
        collection_party_id: str,
        revocation_window_hours: int,
        repayment_schedule: list[dict[str, Any]],
        display_copy: dict[str, Any],
        occurred_at: str,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if state.offer_id is not None:
                return self._reject(command_id, "offer_exists")
            invalid = self._validate_offer_facts(
                command_id, state, terms, parties, collection_party_id,
                repayment_schedule, display_copy,
            )
            if invalid:
                return invalid
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.OFFER_SHOWN, ev.AGGREGATE_CREDIT_OFFER, offer_id,
                occurred_at, f"订单 {order_id} 展示信贷报价第 1 版",
                {
                    "order_id": order_id,
                    "offer_version": 1,
                    "terms": terms,
                    "fees": fees,
                    "parties": parties,
                    "collection_party_id": collection_party_id,
                    "revocation_window_hours": revocation_window_hours,
                    "repayment_schedule": repayment_schedule,
                    "display_copy": display_copy,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def update_credit_offer(
        self,
        command_id: str,
        *,
        order_id: str,
        terms: dict[str, Any],
        fees: list[dict[str, Any]],
        parties: list[dict[str, Any]],
        collection_party_id: str,
        revocation_window_hours: int,
        repayment_schedule: list[dict[str, Any]],
        display_copy: dict[str, Any],
        change_reason: str,
        occurred_at: str,
    ) -> Decision:
        """报价变化：推进报价版本，旧纪元下的逐项确认随之失效。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if state.offer_id is None:
                return self._reject(command_id, "offer_not_found")
            if state.offer_status not in ("shown", "declined"):
                return self._reject(command_id, "offer_not_updatable", {"offer_status": state.offer_status})
            invalid = self._validate_offer_facts(
                command_id, state, terms, parties, collection_party_id,
                repayment_schedule, display_copy,
            )
            if invalid:
                return invalid
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.OFFER_UPDATED, ev.AGGREGATE_CREDIT_OFFER, state.offer_id,
                occurred_at,
                f"订单 {order_id} 报价更新为第 {state.offer_version + 1} 版，需重新确认",
                {
                    "order_id": order_id,
                    "offer_version": state.offer_version + 1,
                    "terms": terms,
                    "fees": fees,
                    "parties": parties,
                    "collection_party_id": collection_party_id,
                    "revocation_window_hours": revocation_window_hours,
                    "repayment_schedule": repayment_schedule,
                    "display_copy": display_copy,
                    "change_reason": change_reason,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    # ------------------------------------------------------------------
    # 适当性评估与人工复核
    # ------------------------------------------------------------------
    def record_suitability(
        self,
        command_id: str,
        *,
        order_id: str,
        risk_level: str,
        anomalies: list[str],
        decided_by: str,
        occurred_at: str,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            status = SUITABILITY_PENDING_REVIEW if anomalies else SUITABILITY_AUTO_CLEAR
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.SUITABILITY_REVIEWED, ev.AGGREGATE_SUITABILITY,
                ev.suitability_id(order_id), occurred_at,
                f"订单 {order_id} 适当性评估完成，风险等级 {risk_level}",
                {
                    "order_id": order_id,
                    "customer_id": state.customer_id,
                    "risk_level": risk_level,
                    "anomalies": list(anomalies),
                    "decided_by": decided_by,
                    "status": status,
                },
            )
            if anomalies:
                self._append(
                    out, command_id, ev.MANUAL_REVIEW_QUEUED, ev.AGGREGATE_SUITABILITY,
                    ev.suitability_id(order_id), occurred_at,
                    f"订单 {order_id} 评估异常，进入人工复核",
                    {
                        "order_id": order_id,
                        "anomalies": list(anomalies),
                        "queued_reason": "suitability_anomaly",
                    },
                )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def resolve_manual_review(
        self,
        command_id: str,
        *,
        order_id: str,
        decision: str,
        reviewer: str,
        occurred_at: str,
        final_risk_level: str | None = None,
        note: str | None = None,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if state.suitability_status != SUITABILITY_PENDING_REVIEW:
                return self._reject(command_id, "no_pending_review")
            if decision not in (SUITABILITY_APPROVED, SUITABILITY_DECLINED):
                return self._reject(command_id, "invalid_decision", {"decision": decision})
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.MANUAL_REVIEW_RESOLVED, ev.AGGREGATE_SUITABILITY,
                ev.suitability_id(order_id), occurred_at,
                f"订单 {order_id} 人工复核结论：{decision}",
                {
                    "order_id": order_id,
                    "decision": decision,
                    "reviewer": reviewer,
                    "final_risk_level": final_risk_level,
                    "note": note,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    # ------------------------------------------------------------------
    # 逐项确认与综合确认
    # ------------------------------------------------------------------
    def confirm_item(
        self,
        command_id: str,
        *,
        order_id: str,
        item_key: str,
        occurred_at: str,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if item_key not in REQUIRED_CONSENT_ITEMS:
                return self._reject(command_id, "unknown_item", {"item_key": item_key})
            if state.offer_id is None:
                return self._reject(command_id, "offer_not_found")
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.ITEM_CONFIRMED, ev.AGGREGATE_CONSENT_RECORD,
                ev.consent_record_id(order_id), occurred_at,
                f"订单 {order_id} 用户逐项确认：{item_key}",
                {
                    "order_id": order_id,
                    "item_key": item_key,
                    "offer_version": state.offer_version,
                    "risk_level": state.risk_level,
                    "actor": "customer",
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def confirm_checkout(
        self,
        command_id: str,
        *,
        order_id: str,
        pay_with_credit: bool,
        occurred_at: str,
        offer_version: int | None = None,
        risk_level: str | None = None,
    ) -> Decision:
        """综合确认：支付总是落账；选择信贷时校验同意纪元后同时签约。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if not pay_with_credit:
                out = []
                self._append_payment(out, command_id, state, occurred_at, funded_by="cash", consent=None)
                return Decision(command_id, True, None, tuple(out))
            if state.offer_id is None:
                return self._reject(command_id, "offer_not_found")
            if state.offer_status == "declined":
                return self._reject(command_id, "offer_declined")
            if state.offer_status != "shown":
                return self._reject(command_id, "offer_not_available", {"offer_status": state.offer_status})
            if offer_version != state.offer_version:
                return self._reject(
                    command_id, "reconfirmation_required",
                    {"current_offer_version": state.offer_version},
                )
            if state.suitability_status is None:
                return self._reject(command_id, "suitability_required")
            if state.suitability_status == SUITABILITY_PENDING_REVIEW:
                return self._reject(command_id, "manual_review_pending")
            if state.suitability_status == SUITABILITY_DECLINED:
                return self._reject(command_id, "suitability_declined")
            if risk_level != state.risk_level:
                return self._reject(
                    command_id, "reconfirmation_required",
                    {"current_risk_level": state.risk_level},
                )
            missing = state.missing_items()
            if missing:
                return self._reject(command_id, "items_missing", {"missing": missing})
            consent = {
                "offer_version": state.offer_version,
                "risk_level": state.risk_level,
                "confirmed_items": sorted(REQUIRED_CONSENT_ITEMS),
            }
            out = []
            self._append_payment(out, command_id, state, occurred_at, funded_by="credit", consent=consent)
            self._append(
                out, command_id, ev.CREDIT_ACCEPTED, ev.AGGREGATE_CREDIT_OFFER, state.offer_id,
                occurred_at, f"订单 {order_id} 用户接受信贷报价第 {state.offer_version} 版",
                {
                    "order_id": order_id,
                    "offer_version": state.offer_version,
                    "customer_id": state.customer_id,
                },
            )
            self._issue_contract(out, command_id, state, occurred_at, consent)
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def _append_payment(
        self,
        out: list[dict[str, Any]],
        command_id: str,
        state: CheckoutState,
        occurred_at: str,
        funded_by: str,
        consent: dict[str, Any] | None,
    ) -> None:
        self._append(
            out, command_id, ev.PAYMENT_CONFIRMED, ev.AGGREGATE_PAYMENT_ORDER, state.order_id,
            occurred_at, f"订单 {state.order_id} 支付成功（{funded_by}）",
            {
                "order_id": state.order_id,
                "payer": state.customer_id,
                "payee": state.merchant_party_id,
                "platform_party_id": state.platform_party_id,
                "amount_minor": state.payable_minor,
                "currency": state.currency,
                "funded_by": funded_by,
                "consent": consent,
            },
        )

    def _issue_contract(
        self,
        out: list[dict[str, Any]],
        command_id: str,
        state: CheckoutState,
        occurred_at: str,
        consent: dict[str, Any],
    ) -> None:
        facts = state.offer_facts
        lender = next(party["party_id"] for party in facts["parties"] if party["role"] == "lender")
        window_until = (
            parse_ts(occurred_at) + timedelta(hours=facts["revocation_window_hours"])
        ).isoformat()
        self._append(
            out, command_id, ev.CONTRACT_ISSUED, ev.AGGREGATE_CREDIT_CONTRACT,
            ev.contract_id_of(state.order_id), occurred_at,
            f"订单 {state.order_id} 信贷合同生效，放款方 {lender}",
            {
                "order_id": state.order_id,
                "offer_id": state.offer_id,
                "offer_version": state.offer_version,
                "customer_id": state.customer_id,
                "lender_party_id": lender,
                "merchant_party_id": state.merchant_party_id,
                "platform_party_id": state.platform_party_id,
                "collection_party_id": facts["collection_party_id"],
                "terms": facts["terms"],
                "currency": state.currency,
                "schedule": facts["repayment_schedule"],
                "revocation_window_hours": facts["revocation_window_hours"],
                "revocation_window_until": window_until,
                "disbursement": {
                    "payer": lender,
                    "payee": state.merchant_party_id,
                    "amount_minor": facts["terms"]["principal_minor"],
                    "currency": state.currency,
                },
                "consent": consent,
            },
        )

    def decline_credit(
        self,
        command_id: str,
        *,
        order_id: str,
        reason: str,
        declined_by: str,
        occurred_at: str,
    ) -> Decision:
        """信贷拒绝：只改变报价状态，订单保持可支付。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._opened_order(command_id, order_id)
            if isinstance(state, Decision):
                return state
            if state.offer_id is None or state.offer_status != "shown":
                return self._reject(
                    command_id, "offer_not_declinable",
                    {"offer_status": state.offer_status},
                )
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.CREDIT_DECLINED, ev.AGGREGATE_CREDIT_OFFER, state.offer_id,
                occurred_at, f"订单 {order_id} 信贷被拒绝，订单不受影响",
                {
                    "order_id": order_id,
                    "reason": reason,
                    "declined_by": declined_by,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    # ------------------------------------------------------------------
    # 撤回、提前结清与账单
    # ------------------------------------------------------------------
    def revoke_consent(
        self,
        command_id: str,
        *,
        order_id: str,
        occurred_at: str,
    ) -> Decision:
        """撤销窗口内撤回：只取消未到期的后续动作，历史账单保留。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._orders.get(order_id)
            if state is None or state.contract_id is None:
                return self._reject(command_id, "no_credit_consent")
            if state.contract_status != CONTRACT_ACTIVE:
                return self._reject(
                    command_id, "contract_not_active",
                    {"contract_status": state.contract_status},
                )
            if parse_ts(occurred_at) > parse_ts(state.revocation_window_until):
                return self._reject(
                    command_id, "revocation_window_expired",
                    {"revocation_window_until": state.revocation_window_until},
                )
            cancelled = [
                entry["seq"]
                for entry in state.schedule
                if entry["status"] == SCHEDULE_SCHEDULED
                and parse_ts(entry["due_at"]) > parse_ts(occurred_at)
            ]
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.CONSENT_REVOKED, ev.AGGREGATE_CONSENT_RECORD,
                ev.consent_record_id(order_id), occurred_at,
                f"订单 {order_id} 用户在撤销窗口内撤回信贷同意",
                {
                    "order_id": order_id,
                    "contract_id": state.contract_id,
                    "revoked_at": occurred_at,
                    "within_window": True,
                    "cancelled_schedule_seqs": cancelled,
                    "preserved_statement_seqs": sorted(state.statements),
                    "collection_party_id": state.contract_facts["collection_party_id"],
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def settle_early(
        self,
        command_id: str,
        *,
        order_id: str,
        settled_amount_minor: int,
        occurred_at: str,
    ) -> Decision:
        """提前结清：关闭后续动作，历史事实保持不变。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._orders.get(order_id)
            if state is None or state.contract_id is None:
                return self._reject(command_id, "no_credit_consent")
            if state.contract_status not in (CONTRACT_ACTIVE, CONTRACT_REVOKED):
                return self._reject(
                    command_id, "contract_not_settleable",
                    {"contract_status": state.contract_status},
                )
            remaining = state.active_schedule()
            remaining_minor = sum(entry["amount_minor"] for entry in remaining)
            if settled_amount_minor != remaining_minor:
                return self._reject(
                    command_id, "amount_mismatch",
                    {"remaining_minor": remaining_minor},
                )
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.EARLY_SETTLED, ev.AGGREGATE_CREDIT_CONTRACT,
                state.contract_id, occurred_at,
                f"订单 {order_id} 提前结清，合同关闭",
                {
                    "order_id": order_id,
                    "settled_amount_minor": settled_amount_minor,
                    "settled_seqs": [entry["seq"] for entry in remaining],
                    "closed_followups": ["new_statements", "new_charges", "repayment_reminders"],
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def generate_statement(
        self,
        command_id: str,
        *,
        order_id: str,
        seq: int,
        occurred_at: str,
    ) -> Decision:
        """出具历史账单：仅合同存续且计划条目有效时可出具。"""
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            state = self._orders.get(order_id)
            if state is None or state.contract_id is None:
                return self._reject(command_id, "no_credit_consent")
            if state.contract_status != CONTRACT_ACTIVE:
                return self._reject(
                    command_id, "contract_not_active",
                    {"contract_status": state.contract_status},
                )
            entry = next((item for item in state.schedule if item["seq"] == seq), None)
            if entry is None:
                return self._reject(command_id, "unknown_schedule_seq", {"seq": seq})
            if entry["status"] != SCHEDULE_SCHEDULED:
                return self._reject(command_id, "entry_not_billable", {"status": entry["status"]})
            if entry["statement_generated"]:
                return self._reject(command_id, "statement_exists", {"seq": seq})
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.STATEMENT_GENERATED, ev.AGGREGATE_CREDIT_CONTRACT,
                state.contract_id, occurred_at,
                f"订单 {order_id} 第 {seq} 期账单出具",
                {
                    "order_id": order_id,
                    "seq": seq,
                    "due_at": entry["due_at"],
                    "amount_minor": entry["amount_minor"],
                    "payee": state.contract_facts["collection_party_id"],
                    "currency": state.currency,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    # ------------------------------------------------------------------
    # 争议处理
    # ------------------------------------------------------------------
    def open_dispute(
        self,
        command_id: str,
        *,
        dispute_id: str,
        order_id: str,
        raised_by: str,
        reason: str,
        occurred_at: str,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            if order_id not in self._orders:
                return self._reject(command_id, "order_not_found")
            if dispute_id in self._disputes:
                return self._reject(command_id, "dispute_exists")
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.DISPUTE_OPENED, ev.AGGREGATE_DISPUTE_CASE, dispute_id,
                occurred_at, f"订单 {order_id} 争议已受理",
                {
                    "order_id": order_id,
                    "raised_by": raised_by,
                    "reason": reason,
                    "stage": "opened",
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def advance_dispute(
        self,
        command_id: str,
        *,
        dispute_id: str,
        stage: str,
        occurred_at: str,
        note: str | None = None,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            dispute = self._disputes.get(dispute_id)
            if dispute is None:
                return self._reject(command_id, "dispute_not_found")
            if dispute.resolved:
                return self._reject(command_id, "dispute_closed")
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.DISPUTE_STAGE_ADVANCED, ev.AGGREGATE_DISPUTE_CASE, dispute_id,
                occurred_at, f"争议 {dispute_id} 进入阶段 {stage}",
                {
                    "order_id": dispute.order_id,
                    "stage": stage,
                    "note": note,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    def resolve_dispute(
        self,
        command_id: str,
        *,
        dispute_id: str,
        outcome: str,
        occurred_at: str,
        note: str | None = None,
    ) -> Decision:
        request = locals().copy()
        request.pop("self")

        def handler() -> Decision:
            dispute = self._disputes.get(dispute_id)
            if dispute is None:
                return self._reject(command_id, "dispute_not_found")
            if dispute.resolved:
                return self._reject(command_id, "dispute_closed")
            out: list[dict[str, Any]] = []
            self._append(
                out, command_id, ev.DISPUTE_RESOLVED, ev.AGGREGATE_DISPUTE_CASE, dispute_id,
                occurred_at, f"争议 {dispute_id} 已解决",
                {
                    "order_id": dispute.order_id,
                    "outcome": outcome,
                    "note": note,
                },
            )
            return Decision(command_id, True, None, tuple(out))

        return self._execute(command_id, request, handler)

    # ------------------------------------------------------------------
    # 查询：重启后的续办与提醒
    # ------------------------------------------------------------------
    def pending_confirmations(self) -> list[dict[str, Any]]:
        """尚未完成综合确认的订单，以及当前纪元下仍缺的确认条目。"""
        pending = []
        for order_id, state in sorted(self._orders.items()):
            if state.order_status != ORDER_OPENED_STATUS:
                continue
            pending.append(
                {
                    "order_id": order_id,
                    "offer_status": state.offer_status,
                    "offer_version": state.offer_version,
                    "risk_level": state.risk_level,
                    "suitability_status": state.suitability_status,
                    "missing_items": state.missing_items() if state.offer_id else list(REQUIRED_CONSENT_ITEMS),
                }
            )
        return pending

    def pending_manual_reviews(self) -> list[dict[str, Any]]:
        """评估异常待人工复核的订单。"""
        return [
            {"order_id": order_id, "anomalies": list(state.anomalies)}
            for order_id, state in sorted(self._orders.items())
            if state.suitability_status == SUITABILITY_PENDING_REVIEW
        ]

    def due_reminders(self, now: str, within_hours: int = 24) -> list[dict[str, Any]]:
        """到期提醒：仍有效的还款计划中，窗口内到期（含已逾期）的条目。"""
        moment = parse_ts(now)
        horizon = moment + timedelta(hours=within_hours)
        reminders = []
        for order_id, state in sorted(self._orders.items()):
            if state.contract_id is None:
                continue
            payee = state.contract_facts.get("collection_party_id")
            for entry in state.schedule:
                if entry["status"] != SCHEDULE_SCHEDULED:
                    continue
                if parse_ts(entry["due_at"]) <= horizon:
                    reminders.append(
                        {
                            "order_id": order_id,
                            "contract_id": state.contract_id,
                            "seq": entry["seq"],
                            "due_at": entry["due_at"],
                            "amount_minor": entry["amount_minor"],
                            "payee": payee,
                        }
                    )
        return sorted(reminders, key=lambda item: (item["due_at"], item["order_id"], item["seq"]))

    def order_state(self, order_id: str) -> CheckoutState | None:
        return self._orders.get(order_id)

    def dispute_state(self, dispute_id: str) -> DisputeState | None:
        return self._disputes.get(dispute_id)

    def events_for_order(self, order_id: str) -> list[dict[str, Any]]:
        """一笔交易跨聚合的全部事件，按入账顺序排列。"""
        return [
            self._store.get(event_id)
            for event_id in self._order_events.get(order_id, [])
        ]
