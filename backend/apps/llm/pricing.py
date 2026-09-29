"""冻结峰谷价与日历；预留按峰价，结算只读调用账本快照。"""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils import timezone

from apps.common.http import api_error
from apps.common.idempotency import request_fingerprint
from apps.llm.schemas import ModelUsage

PRICE_KEYS = ("input_cache_hit_per_million_usd", "input_cache_miss_per_million_usd", "output_per_million_usd")
UNIT = Decimal("0.00000001")
MILLION = Decimal(1_000_000)


def pricing_snapshot(*, mock: bool, at: datetime | None = None) -> dict:
    moment = (at or timezone.now()).astimezone(UTC)
    rates = settings.LLM_PRICING
    if not mock:
        if not settings.LLM_PRICING_VERSION or settings.LLM_PRICING_CALENDAR_YEAR != moment.year:
            raise api_error("SERVICE_UNAVAILABLE", "真实调用需要核验价目版本和当年峰谷日历")
        for key in PRICE_KEYS:
            peak, off_peak = rates["peak"][key], rates["off_peak"][key]
            if not peak.is_finite() or not off_peak.is_finite() or not 0 < off_peak <= peak:
                raise api_error("SERVICE_UNAVAILABLE", "真实调用需要有效的六项峰谷单价")
    holiday = moment.date().isoformat() in settings.LLM_PRICING_OFF_PEAK_DATES_UTC
    peak_hour = 1 <= moment.hour < 4 or 6 <= moment.hour < 10
    tariff = "peak" if moment.weekday() < 5 and peak_hour and not holiday else "off_peak"
    table = {
        band: {key: str(Decimal(0) if mock else rates[band][key]) for key in PRICE_KEYS}
        for band in ("peak", "off_peak")
    }
    material = {
        "rates": table,
        "calendar_year": settings.LLM_PRICING_CALENDAR_YEAR,
        "off_peak_dates_utc": list(settings.LLM_PRICING_OFF_PEAK_DATES_UTC),
        "version": settings.LLM_PRICING_VERSION,
        "model": settings.DEEPSEEK_MODEL,
    }
    return {
        **material,
        "configuration_hash": request_fingerprint(material),
        "tariff": tariff,
        "priced_at": moment.isoformat(),
        "basis": "request_started_at_utc",
        "mock": mock,
    }


def cost(usage: ModelUsage, snapshot: dict, *, reserve: bool = False) -> Decimal | None:
    for value in (usage.input_tokens, usage.output_tokens, usage.cache_hit_tokens, usage.cache_miss_tokens):
        if value is not None and (type(value) is not int or not 0 <= value <= 10**12):
            return None
    if snapshot["mock"]:
        return Decimal(0)
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    rates = snapshot["rates"]["peak" if reserve else snapshot["tariff"]]
    if reserve:
        # 未知是否命中缓存；所有输入按峰时较高单价预留。
        incoming = usage.input_tokens * max(Decimal(rates[k]) for k in PRICE_KEYS[:2])
    else:
        if usage.cache_hit_tokens is None or usage.cache_miss_tokens is None:
            return None
        if usage.cache_hit_tokens + usage.cache_miss_tokens != usage.input_tokens:
            return None
        incoming = usage.cache_hit_tokens * Decimal(rates[PRICE_KEYS[0]]) + usage.cache_miss_tokens * Decimal(
            rates[PRICE_KEYS[1]]
        )
    amount = (incoming + usage.output_tokens * Decimal(rates[PRICE_KEYS[2]])) / MILLION
    return amount.quantize(UNIT, rounding=ROUND_CEILING if reserve else ROUND_HALF_UP)
