"""支付与信贷同意账本的核心服务。

把商品支付、信贷报价、逐项同意和争议处理放进同一个仅追加账本：
支付可以在没有信贷时完成，信贷拒绝不改变订单；同一请求重试返回原决定；
报价或风险等级变化后必须基于最新页面重新确认；服务重启后可以从事件日志
恢复未完成的确认、人工复核队列和到期提醒。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import views
from .ledger import EventStore, StoredEvent

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

# 确认动作里必须逐项同意的条款。
REQUIRED_CONSENT_ITEMS = (
    "comprehensive_cost",  # 综合成本
    "risk_disclosure",  # 风险提示
    "contract_parties",  # 合同参与方
    "revocation_window",  # 撤销窗口
)

# 争议处理阶段，只允许顺序前进。
DISPUTE_STAGES = ("accepted", "investigating", "mediating", "resolved", "closed")
TERMINAL_DISPUTE_STAGES = ("resolved", "closed")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _offer_id(order_id: str) -> str:
    return f"offer-{order_id}"


def _consent_id(order_id: str) -> str:
    return f"consent-{order_id}"


def _quote_fingerprint(fee_breakdown: Mapping[str, Any], risk_level: str) -> str:
    """报价指纹：费用明细或风险等级任一变化都视为新报价。"""
    return json.dumps(
        {"fee_breakdown": fee_breakdown, "risk_level": risk_level},
        ensure_ascii=False,
        sort_keys=True,
    )


def _build_schedule(total_cost: float, installments: int, from_moment: datetime) -> list[dict[str, Any]]:
    """按 30 天一期生成等额还款计划，最后一期吸收舍入差。"""
    count = max(1, int(installments))
    base = round(total_cost / count, 2)
    schedule = []
    for seq in range(1, count + 1):
        amount = base if seq < count else round(total_cost - base * (count - 1), 2)
        schedule.append(
            {
                "seq": seq,
                "due_at": _iso(from_moment + timedelta(days=30 * seq)),
                "amount": amount,
                "settled": False,
            }
        )
    return schedule


@dataclass(frozen=True)
class Decision:
    """一次命令的处理结果；同一 request_id 重试时原样返回并标记 replayed。"""

    ok: bool
    code: str
    summary: str
    events: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False


@dataclass
class _OfferState:
    offer_version: int
    status: str  # shown / superseded / accepted / rejected
    fee_breakdown: dict[str, Any]
    risk_level: str
    lender: Any
    funding_party: Any
    collection_party: Any
    display: dict[str, Any]
    revocation_window_hours: int
    installments: int
    shown_at: str
    expires_at: str
    fingerprint: str
    suitability: str = "pending"  # pending / approved / rejected / manual_review
    suitability_detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ConsentState:
    offer_version: int
    display_version: str
    items: dict[str, bool]
    confirmed_by: str
    given_at: str
    revocation_window_hours: int
    revoked: bool = False
    revoked_at: str | None = None
    revoke_reason: str = ""


@dataclass
class _CreditState:
    agreement_id: str
    status: str  # active / revoked / settled
    lender: Any
    funding_party: Any
    collection_party: Any
    disbursement: dict[str, Any]
    schedule: list[dict[str, Any]]


@dataclass
class _OrderState:
    order_id: str
    amount: float
    currency: str
    items: list[dict[str, Any]]
    status: str = "created"  # created / paid
    payment: dict[str, Any] | None = None
    offers: list[_OfferState] = field(default_factory=list)
    consent: _ConsentState | None = None
    credit: _CreditState | None = None

    @property
    def current_offer(self) -> _OfferState | None:
        return self.offers[-1] if self.offers else None


@dataclass
class _DisputeState:
    dispute_id: str
    order_id: str
    topic: str
    stage: str
    opened_by: str
    history: list[dict[str, Any]] = field(default_factory=list)


class ConsentLedger:
    """账本门面：命令写事件，查询读投影，重启后从事件完整恢复。"""

    def __init__(
        self,
        store: EventStore,
        clock: Callable[[], datetime] | None = None,
        requests_path: str | Path | None = None,
        requests: Mapping[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or _utc_now
        self._requests_path = Path(requests_path) if requests_path else None
        self._requests: dict[str, dict[str, Any]] = dict(requests or {})
        self._orders: dict[str, _OrderState] = {}
        self._disputes: dict[str, _DisputeState] = {}
        for event in self._store.events():
            self._apply(event)

    @classmethod
    def open(
        cls,
        directory: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        schema_path: str | Path | None = None,
    ) -> "ConsentLedger":
        """打开（必要时创建）一个账本目录，并恢复其中的全部历史。"""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        schema_file = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
        schema = json.loads(schema_file.read_text(encoding="utf-8")) if schema_file.exists() else None
        store = EventStore(directory / "events.jsonl", schema=schema)
        requests_file = directory / "requests.json"
        requests = json.loads(requests_file.read_text(encoding="utf-8")) if requests_file.exists() else {}
        return cls(store, clock=clock, requests_path=requests_file, requests=requests)

    # ------------------------------------------------------------------
    # 基础设施：幂等与事件写入
    # ------------------------------------------------------------------

    def _run(self, request_id: str, action: Callable[[], Decision]) -> Decision:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id 必须是非空字符串")
        stored = self._requests.get(request_id)
        if stored is not None:
            return Decision(
                ok=stored["ok"],
                code=stored["code"],
                summary=stored["summary"],
                events=tuple(stored["events"]),
                detail=dict(stored["detail"]),
                replayed=True,
            )
        decision = action()
        self._requests[request_id] = {
            "ok": decision.ok,
            "code": decision.code,
            "summary": decision.summary,
            "events": [dict(event) for event in decision.events],
            "detail": decision.detail,
        }
        self._persist_requests()
        return decision

    def _persist_requests(self) -> None:
        if self._requests_path:
            self._requests_path.write_text(
                json.dumps(self._requests, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event = self._store.append(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=_iso(self._clock()),
            summary=summary,
            payload=payload,
        )
        self._apply(event)
        return event.to_envelope()

    def _require_order(self, order_id: str) -> _OrderState:
        order = self._orders.get(order_id)
        if order is None:
            raise LookupError(f"订单不存在：{order_id}")
        return order

    def _find_offer(self, order_id: str, offer_version: int) -> _OfferState | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        for offer in order.offers:
            if offer.offer_version == offer_version:
                return offer
        return None

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def create_order(
        self,
        request_id: str,
        order_id: str,
        *,
        items: list[Mapping[str, Any]],
        amount: float,
        currency: str = "CNY",
    ) -> Decision:
        def action() -> Decision:
            if order_id in self._orders:
                return Decision(False, "order_exists", f"订单 {order_id} 已存在")
            envelope = self._emit(
                "ORDER_CREATED",
                "payment_order",
                order_id,
                f"创建订单 {order_id}",
                {"order_id": order_id, "items": [dict(item) for item in items], "amount": amount, "currency": currency},
            )
            return Decision(True, "order_created", f"订单 {order_id} 已创建", (envelope,), {"order_id": order_id})

        return self._run(request_id, action)

    def show_offer(
        self,
        request_id: str,
        order_id: str,
        *,
        fee_breakdown: Mapping[str, Any],
        risk_level: str,
        lender: Any,
        funding_party: Any,
        collection_party: Any,
        display: Mapping[str, Any],
        revocation_window_hours: int = 24,
        installments: int = 3,
        valid_hours: int = 72,
    ) -> Decision:
        """展示信贷报价；报价或风险等级变化时作废旧版本并生成新版本。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            if order.status != "created":
                return Decision(False, "order_already_paid", "订单已完成支付，不能再展示报价")
            if not display.get("display_version") or not display.get("copies"):
                return Decision(False, "display_incomplete", "展示快照必须包含 display_version 和 copies")
            fee = dict(fee_breakdown)
            fingerprint = _quote_fingerprint(fee, risk_level)
            current = order.current_offer
            if current and current.status == "shown" and current.fingerprint == fingerprint:
                return Decision(
                    True,
                    "offer_unchanged",
                    "报价与风险等级未变化，沿用当前展示",
                    (),
                    {"order_id": order_id, "offer_version": current.offer_version},
                )
            events: list[dict[str, Any]] = []
            if current and current.status == "shown":
                reason = "quote_changed" if current.fee_breakdown != fee else "risk_level_changed"
                events.append(
                    self._emit(
                        "OFFER_SUPERSEDED",
                        "credit_offer",
                        _offer_id(order_id),
                        f"报价版本 {current.offer_version} 被取代（{reason}）",
                        {"order_id": order_id, "offer_version": current.offer_version, "reason": reason},
                    )
                )
            offer_version = (current.offer_version + 1) if current else 1
            now = self._clock()
            events.append(
                self._emit(
                    "OFFER_SHOWN",
                    "credit_offer",
                    _offer_id(order_id),
                    f"展示信贷报价版本 {offer_version}",
                    {
                        "order_id": order_id,
                        "offer_version": offer_version,
                        "fee_breakdown": fee,
                        "risk_level": risk_level,
                        "lender": lender,
                        "funding_party": funding_party,
                        "collection_party": collection_party,
                        "display": dict(display),
                        "revocation_window_hours": revocation_window_hours,
                        "installments": installments,
                        "shown_at": _iso(now),
                        "expires_at": _iso(now + timedelta(hours=valid_hours)),
                    },
                )
            )
            return Decision(
                True,
                "offer_shown",
                f"已展示报价版本 {offer_version}",
                tuple(events),
                {"order_id": order_id, "offer_version": offer_version},
            )

        return self._run(request_id, action)

    def review_suitability(
        self,
        request_id: str,
        order_id: str,
        *,
        outcome: str,
        reasons: list[str] | tuple[str, ...] = (),
        decided_by: str = "system:suitability-rule",
    ) -> Decision:
        """适当性评估；结果为 manual_review 时进入人工复核队列。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            offer = order.current_offer
            if offer is None or offer.status != "shown":
                return Decision(False, "offer_not_open", "当前没有待评估的报价")
            if offer.suitability != "pending":
                return Decision(False, "suitability_already_decided", "该报价已完成适当性评估")
            if outcome not in ("approved", "rejected", "manual_review"):
                return Decision(False, "unsupported_outcome", f"不支持的评估结果：{outcome}")
            events = [
                self._emit(
                    "SUITABILITY_REVIEWED",
                    "credit_offer",
                    _offer_id(order_id),
                    f"适当性评估结果：{outcome}",
                    {
                        "order_id": order_id,
                        "offer_version": offer.offer_version,
                        "outcome": outcome,
                        "reasons": list(reasons),
                        "decided_by": decided_by,
                    },
                )
            ]
            if outcome == "rejected":
                events.append(
                    self._emit(
                        "CREDIT_REJECTED",
                        "credit_offer",
                        _offer_id(order_id),
                        "信贷被拒绝，订单本身不受影响",
                        {"order_id": order_id, "offer_version": offer.offer_version, "decided_by": decided_by},
                    )
                )
            return Decision(
                True,
                f"suitability_{outcome}",
                f"评估完成：{outcome}",
                tuple(events),
                {"order_id": order_id, "offer_version": offer.offer_version, "outcome": outcome},
            )

        return self._run(request_id, action)

    def resolve_manual_review(
        self,
        request_id: str,
        order_id: str,
        *,
        reviewer: str,
        approved: bool,
        note: str = "",
    ) -> Decision:
        """人工复核结论；拒绝时订单保持不变。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            offer = order.current_offer
            if offer is None or offer.suitability != "manual_review":
                return Decision(False, "no_manual_review_pending", "当前没有待复核的评估")
            outcome = "approved" if approved else "rejected"
            events = [
                self._emit(
                    "MANUAL_REVIEW_RESOLVED",
                    "credit_offer",
                    _offer_id(order_id),
                    f"人工复核结论：{outcome}",
                    {
                        "order_id": order_id,
                        "offer_version": offer.offer_version,
                        "reviewer": reviewer,
                        "outcome": outcome,
                        "note": note,
                    },
                )
            ]
            if not approved:
                events.append(
                    self._emit(
                        "CREDIT_REJECTED",
                        "credit_offer",
                        _offer_id(order_id),
                        "人工复核拒绝信贷，订单本身不受影响",
                        {"order_id": order_id, "offer_version": offer.offer_version, "decided_by": reviewer},
                    )
                )
            return Decision(
                True,
                f"manual_review_{outcome}",
                f"人工复核完成：{outcome}",
                tuple(events),
                {"order_id": order_id, "outcome": outcome},
            )

        return self._run(request_id, action)

    def confirm(
        self,
        request_id: str,
        order_id: str,
        *,
        use_credit: bool = True,
        offer_version: int | None = None,
        display_version: str | None = None,
        consent_items: Mapping[str, bool] | None = None,
        confirmed_by: str = "",
    ) -> Decision:
        """确认动作：可以不使用信贷直接支付；使用信贷时要求报价、评估和逐项同意都有效。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            if order.status != "created":
                return Decision(False, "order_already_paid", "订单已完成支付，不能重复确认")
            if not use_credit:
                envelope = self._emit(
                    "PAYMENT_CONFIRMED",
                    "payment_order",
                    order_id,
                    "不使用信贷完成支付",
                    {
                        "order_id": order_id,
                        "amount": order.amount,
                        "currency": order.currency,
                        "method": "balance",
                        "payee": "platform_merchant",
                        "funded_by": None,
                    },
                )
                return Decision(
                    True,
                    "payment_confirmed",
                    "支付完成（未使用信贷）",
                    (envelope,),
                    {"order_id": order_id, "method": "balance"},
                )
            offer = order.current_offer
            if offer is None:
                return Decision(False, "no_offer", "当前没有可用的信贷报价")
            if offer.suitability == "rejected":
                return Decision(False, "credit_rejected", "信贷已被拒绝，订单本身不受影响")
            if offer.status != "shown":
                return Decision(False, "offer_not_open", f"报价当前状态为 {offer.status}，不能确认")
            if offer_version != offer.offer_version or display_version != offer.display.get("display_version"):
                return Decision(
                    False,
                    "reconfirmation_required",
                    "报价或展示版本已变化，必须基于最新页面重新确认，不能沿用旧同意",
                    (),
                    {"current_offer_version": offer.offer_version, "current_display_version": offer.display.get("display_version")},
                )
            if offer.suitability == "manual_review":
                return Decision(False, "manual_review_pending", "适当性评估正在人工复核，暂不能确认")
            if offer.suitability != "approved":
                return Decision(False, "suitability_pending", "适当性评估尚未完成")
            if _parse(offer.expires_at) <= self._clock():
                return Decision(False, "offer_expired", "报价已过期，需要重新展示")
            items = dict(consent_items or {})
            missing = [key for key in REQUIRED_CONSENT_ITEMS if items.get(key) is not True]
            if missing:
                return Decision(False, "consent_incomplete", "存在未确认的必选条款", (), {"missing": missing})
            now = self._clock()
            events = [
                self._emit(
                    "CONSENT_GIVEN",
                    "consent_record",
                    _consent_id(order_id),
                    "用户逐项确认信贷条款",
                    {
                        "order_id": order_id,
                        "offer_version": offer.offer_version,
                        "display_version": display_version,
                        "items": {key: True for key in REQUIRED_CONSENT_ITEMS},
                        "confirmed_by": confirmed_by,
                        "revocation_window_hours": offer.revocation_window_hours,
                    },
                ),
                self._emit(
                    "PAYMENT_CONFIRMED",
                    "payment_order",
                    order_id,
                    "使用信贷完成支付",
                    {
                        "order_id": order_id,
                        "amount": order.amount,
                        "currency": order.currency,
                        "method": "credit",
                        "payee": "platform_merchant",
                        "funded_by": offer.funding_party,
                    },
                ),
            ]
            agreement_id = f"agreement-{order_id}"
            events.append(
                self._emit(
                    "CREDIT_ACCEPTED",
                    "credit_offer",
                    _offer_id(order_id),
                    "信贷合同生效并放款",
                    {
                        "order_id": order_id,
                        "offer_version": offer.offer_version,
                        "agreement_id": agreement_id,
                        "lender": offer.lender,
                        "funding_party": offer.funding_party,
                        "collection_party": offer.collection_party,
                        "disbursement": {"amount": order.amount, "to": "platform_merchant", "at": _iso(now)},
                        "schedule": _build_schedule(
                            offer.fee_breakdown.get("total_cost", order.amount), offer.installments, now
                        ),
                    },
                )
            )
            return Decision(
                True,
                "credit_confirmed",
                "支付与信贷确认完成",
                tuple(events),
                {"order_id": order_id, "agreement_id": agreement_id},
            )

        return self._run(request_id, action)

    def revoke_consent(self, request_id: str, order_id: str, *, reason: str = "") -> Decision:
        """在撤销窗口内撤回同意；只影响后续动作，历史账单与收款主体保留可审计。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            consent = order.consent
            if consent is None or consent.revoked:
                return Decision(False, "no_active_consent", "没有可撤回的同意")
            deadline = _parse(consent.given_at) + timedelta(hours=consent.revocation_window_hours)
            if self._clock() > deadline:
                return Decision(
                    False,
                    "revocation_window_expired",
                    "已超出撤销窗口",
                    (),
                    {"deadline": _iso(deadline)},
                )
            envelope = self._emit(
                "CONSENT_REVOKED",
                "consent_record",
                _consent_id(order_id),
                "用户在窗口期内撤回同意",
                {"order_id": order_id, "reason": reason, "deadline": _iso(deadline)},
            )
            return Decision(
                True,
                "consent_revoked",
                "同意已撤回；历史账单与收款主体保留可审计",
                (envelope,),
                {"order_id": order_id},
            )

        return self._run(request_id, action)

    def early_settle(self, request_id: str, order_id: str) -> Decision:
        """提前结清：结清剩余全部期款，历史还款计划保留。"""

        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            credit = order.credit
            if credit is None or credit.status != "active":
                return Decision(False, "credit_not_active", "没有处于生效中的信贷合同")
            remaining = round(sum(item["amount"] for item in credit.schedule if not item["settled"]), 2)
            envelope = self._emit(
                "EARLY_SETTLED",
                "credit_offer",
                _offer_id(order_id),
                "提前结清全部剩余期款",
                {
                    "order_id": order_id,
                    "agreement_id": credit.agreement_id,
                    "settled_amount": remaining,
                    "settled_at": _iso(self._clock()),
                },
            )
            return Decision(
                True,
                "early_settled",
                "已提前结清；历史账单保留可审计",
                (envelope,),
                {"order_id": order_id, "settled_amount": remaining},
            )

        return self._run(request_id, action)

    def open_dispute(
        self,
        request_id: str,
        order_id: str,
        *,
        topic: str,
        opened_by: str,
        note: str = "",
    ) -> Decision:
        def action() -> Decision:
            order = self._orders.get(order_id)
            if order is None:
                return Decision(False, "order_not_found", f"订单 {order_id} 不存在")
            for dispute in self._disputes.values():
                if dispute.order_id == order_id and dispute.stage not in TERMINAL_DISPUTE_STAGES:
                    return Decision(
                        False,
                        "dispute_already_open",
                        "该订单已有进行中的争议",
                        (),
                        {"dispute_id": dispute.dispute_id},
                    )
            seq = sum(1 for dispute in self._disputes.values() if dispute.order_id == order_id) + 1
            dispute_id = f"dispute-{order_id}-{seq}"
            envelope = self._emit(
                "DISPUTE_OPENED",
                "dispute_case",
                dispute_id,
                f"争议立案：{topic}",
                {"order_id": order_id, "topic": topic, "opened_by": opened_by, "stage": "accepted", "note": note},
            )
            return Decision(True, "dispute_opened", "争议已立案", (envelope,), {"dispute_id": dispute_id})

        return self._run(request_id, action)

    def advance_dispute(
        self,
        request_id: str,
        dispute_id: str,
        *,
        stage: str,
        handler: str,
        note: str = "",
    ) -> Decision:
        def action() -> Decision:
            dispute = self._disputes.get(dispute_id)
            if dispute is None:
                return Decision(False, "dispute_not_found", f"争议 {dispute_id} 不存在")
            if stage not in DISPUTE_STAGES:
                return Decision(False, "unsupported_stage", f"未知争议阶段：{stage}")
            current_index = DISPUTE_STAGES.index(dispute.stage)
            if DISPUTE_STAGES.index(stage) != current_index + 1:
                following = DISPUTE_STAGES[current_index + 1] if current_index + 1 < len(DISPUTE_STAGES) else "（无）"
                return Decision(
                    False,
                    "invalid_stage_transition",
                    f"争议阶段只能从 {dispute.stage} 前进到 {following}",
                )
            envelope = self._emit(
                "DISPUTE_ADVANCED",
                "dispute_case",
                dispute_id,
                f"争议推进到 {stage}",
                {"order_id": dispute.order_id, "stage": stage, "handler": handler, "note": note},
            )
            return Decision(
                True,
                "dispute_advanced",
                "争议阶段已更新",
                (envelope,),
                {"dispute_id": dispute_id, "stage": stage},
            )

        return self._run(request_id, action)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def pending_work(self, *, now: datetime | None = None, reminder_horizon_days: int = 7) -> dict[str, Any]:
        """重启后的待办：未完成的确认、人工复核队列和到期提醒。"""
        moment = now or self._clock()
        horizon = moment + timedelta(days=reminder_horizon_days)
        confirmations: list[dict[str, Any]] = []
        manual_reviews: list[dict[str, Any]] = []
        reminders: list[dict[str, Any]] = []
        for order in self._orders.values():
            offer = order.current_offer
            if order.status == "created" and offer is not None and offer.status == "shown":
                confirmations.append(
                    {
                        "order_id": order.order_id,
                        "offer_version": offer.offer_version,
                        "suitability": offer.suitability,
                        "expires_at": offer.expires_at,
                        "expired": _parse(offer.expires_at) <= moment,
                    }
                )
                if offer.suitability == "manual_review":
                    manual_reviews.append({"order_id": order.order_id, "offer_version": offer.offer_version})
            credit = order.credit
            if credit is not None and credit.status == "active":
                for item in credit.schedule:
                    if not item["settled"] and _parse(item["due_at"]) <= horizon:
                        reminders.append(
                            {
                                "order_id": order.order_id,
                                "agreement_id": credit.agreement_id,
                                "seq": item["seq"],
                                "due_at": item["due_at"],
                                "amount": item["amount"],
                                "collection_party": credit.collection_party,
                            }
                        )
        confirmations.sort(key=lambda entry: entry["order_id"])
        reminders.sort(key=lambda entry: (entry["due_at"], entry["order_id"]))
        return {
            "pending_confirmations": confirmations,
            "manual_reviews": manual_reviews,
            "due_reminders": reminders,
        }

    def view(self, role: str, order_id: str) -> dict[str, Any]:
        """按角色裁剪的订单视图；角色之外没有额外数据通道。"""
        order = self._require_order(order_id)
        return views.project_for_role(role, self._full_view(order))

    def reconstruct(self, order_id: str) -> dict[str, Any]:
        """监管视角：还原展示了什么、谁作出决定、资金流向谁、争议到哪一步。"""
        self._require_order(order_id)
        related_ids = {order_id, _offer_id(order_id), _consent_id(order_id)}
        events = [
            event.to_envelope()
            for event in self._store.events()
            if event.aggregate_id in related_ids
            or (event.aggregate_type == "dispute_case" and event.payload.get("order_id") == order_id)
        ]
        return views.reconstruct(order_id, events)

    def _full_view(self, order: _OrderState) -> dict[str, Any]:
        offer = order.current_offer
        consent = order.consent
        credit = order.credit
        dispute = next((item for item in self._disputes.values() if item.order_id == order.order_id), None)
        return {
            "order": {
                "order_id": order.order_id,
                "amount": order.amount,
                "currency": order.currency,
                "items": [dict(item) for item in order.items],
                "status": order.status,
                "payment": dict(order.payment) if order.payment else None,
            },
            "offer": None
            if offer is None
            else {
                "offer_version": offer.offer_version,
                "status": offer.status,
                "fee_breakdown": dict(offer.fee_breakdown),
                "risk_level": offer.risk_level,
                "lender": offer.lender,
                "funding_party": offer.funding_party,
                "collection_party": offer.collection_party,
                "display": dict(offer.display),
                "revocation_window_hours": offer.revocation_window_hours,
                "installments": offer.installments,
                "shown_at": offer.shown_at,
                "expires_at": offer.expires_at,
                "suitability": offer.suitability,
                "suitability_detail": dict(offer.suitability_detail),
            },
            "consent": None
            if consent is None
            else {
                "offer_version": consent.offer_version,
                "display_version": consent.display_version,
                "items": dict(consent.items),
                "confirmed_by": consent.confirmed_by,
                "given_at": consent.given_at,
                "revocation_window_hours": consent.revocation_window_hours,
                "revoked": consent.revoked,
                "revoked_at": consent.revoked_at,
                "revoke_reason": consent.revoke_reason,
            },
            "credit": None
            if credit is None
            else {
                "agreement_id": credit.agreement_id,
                "status": credit.status,
                "lender": credit.lender,
                "funding_party": credit.funding_party,
                "collection_party": credit.collection_party,
                "disbursement": dict(credit.disbursement),
                "schedule": [dict(item) for item in credit.schedule],
            },
            "dispute": None
            if dispute is None
            else {
                "dispute_id": dispute.dispute_id,
                "topic": dispute.topic,
                "stage": dispute.stage,
                "history": [dict(entry) for entry in dispute.history],
            },
        }

    # ------------------------------------------------------------------
    # 投影：从事件重建状态（重启恢复也走同一条路径）
    # ------------------------------------------------------------------

    def _apply(self, event: StoredEvent) -> None:
        payload = event.payload
        event_type = event.event_type
        if event_type == "ORDER_CREATED":
            self._orders[payload["order_id"]] = _OrderState(
                order_id=payload["order_id"],
                amount=payload["amount"],
                currency=payload["currency"],
                items=[dict(item) for item in payload["items"]],
            )
        elif event_type == "PAYMENT_CONFIRMED":
            order = self._orders[payload["order_id"]]
            order.status = "paid"
            order.payment = dict(payload)
        elif event_type == "OFFER_SHOWN":
            order = self._orders[payload["order_id"]]
            order.offers.append(
                _OfferState(
                    offer_version=payload["offer_version"],
                    status="shown",
                    fee_breakdown=dict(payload["fee_breakdown"]),
                    risk_level=payload["risk_level"],
                    lender=payload["lender"],
                    funding_party=payload["funding_party"],
                    collection_party=payload["collection_party"],
                    display=dict(payload["display"]),
                    revocation_window_hours=payload["revocation_window_hours"],
                    installments=payload["installments"],
                    shown_at=payload["shown_at"],
                    expires_at=payload["expires_at"],
                    fingerprint=_quote_fingerprint(payload["fee_breakdown"], payload["risk_level"]),
                )
            )
        elif event_type == "OFFER_SUPERSEDED":
            offer = self._find_offer(payload["order_id"], payload["offer_version"])
            if offer is not None:
                offer.status = "superseded"
        elif event_type == "SUITABILITY_REVIEWED":
            offer = self._find_offer(payload["order_id"], payload["offer_version"])
            if offer is not None:
                offer.suitability = payload["outcome"]
                offer.suitability_detail = {
                    "reasons": list(payload.get("reasons", [])),
                    "decided_by": payload["decided_by"],
                    "at": event.occurred_at,
                }
        elif event_type == "MANUAL_REVIEW_RESOLVED":
            offer = self._find_offer(payload["order_id"], payload["offer_version"])
            if offer is not None:
                offer.suitability = payload["outcome"]
                offer.suitability_detail = {
                    "reviewer": payload["reviewer"],
                    "note": payload.get("note", ""),
                    "at": event.occurred_at,
                }
        elif event_type == "CREDIT_REJECTED":
            offer = self._find_offer(payload["order_id"], payload["offer_version"])
            if offer is not None:
                offer.status = "rejected"
        elif event_type == "CONSENT_GIVEN":
            order = self._orders[payload["order_id"]]
            order.consent = _ConsentState(
                offer_version=payload["offer_version"],
                display_version=payload["display_version"],
                items=dict(payload["items"]),
                confirmed_by=payload["confirmed_by"],
                given_at=event.occurred_at,
                revocation_window_hours=payload["revocation_window_hours"],
            )
        elif event_type == "CONSENT_REVOKED":
            order = self._orders[payload["order_id"]]
            if order.consent is not None:
                order.consent.revoked = True
                order.consent.revoked_at = event.occurred_at
                order.consent.revoke_reason = payload.get("reason", "")
            if order.credit is not None and order.credit.status == "active":
                order.credit.status = "revoked"
        elif event_type == "CREDIT_ACCEPTED":
            order = self._orders[payload["order_id"]]
            offer = self._find_offer(payload["order_id"], payload["offer_version"])
            if offer is not None:
                offer.status = "accepted"
            order.credit = _CreditState(
                agreement_id=payload["agreement_id"],
                status="active",
                lender=payload["lender"],
                funding_party=payload["funding_party"],
                collection_party=payload["collection_party"],
                disbursement=dict(payload["disbursement"]),
                schedule=[dict(item) for item in payload["schedule"]],
            )
        elif event_type == "EARLY_SETTLED":
            order = self._orders[payload["order_id"]]
            if order.credit is not None:
                order.credit.status = "settled"
                for item in order.credit.schedule:
                    item["settled"] = True
        elif event_type == "DISPUTE_OPENED":
            self._disputes[event.aggregate_id] = _DisputeState(
                dispute_id=event.aggregate_id,
                order_id=payload["order_id"],
                topic=payload["topic"],
                stage=payload["stage"],
                opened_by=payload["opened_by"],
                history=[
                    {
                        "stage": payload["stage"],
                        "handler": payload["opened_by"],
                        "note": payload.get("note", ""),
                        "at": event.occurred_at,
                    }
                ],
            )
        elif event_type == "DISPUTE_ADVANCED":
            dispute = self._disputes[event.aggregate_id]
            dispute.stage = payload["stage"]
            dispute.history.append(
                {
                    "stage": payload["stage"],
                    "handler": payload["handler"],
                    "note": payload.get("note", ""),
                    "at": event.occurred_at,
                }
            )
