from enum import Enum

class PlanTier(str, Enum):
    SINGLE = "single"
    BULK = "bulk"
    ENTERPRISE = "enterprise"

PRICES = {
    PlanTier.SINGLE: 200,  # KES per search
    PlanTier.BULK: {
        "up_to_100": 80,
        "101_500": 50,
        "501_2000": 35,
        "2001_10000": 25,
    }
}

ENTERPRISE_PLANS = {
    "starter": {"monthly_fee": 30000, "included_searches": 5000, "overage_rate": 5},
    "pro": {"monthly_fee": 60000, "included_searches": 15000, "overage_rate": 3},
    "enterprise": {"monthly_fee": 100000, "included_searches": 50000, "overage_rate": 2},
}

def calculate_bulk_amount(name_count: int) -> int:
    if name_count <= 100:
        return name_count * PRICES[PlanTier.BULK]["up_to_100"]
    elif name_count <= 500:
        return name_count * PRICES[PlanTier.BULK]["101_500"]
    elif name_count <= 2000:
        return name_count * PRICES[PlanTier.BULK]["501_2000"]
    else:
        return name_count * PRICES[PlanTier.BULK]["2001_10000"]

def get_enterprise_plan(plan_name: str):
    return ENTERPRISE_PLANS.get(plan_name)