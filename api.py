import os
import uuid
import random
import httpx
from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from datetime import datetime, timedelta
from pricing import PRICES, PlanTier, calculate_bulk_amount, get_enterprise_plan

load_dotenv()

app = FastAPI()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ---------- Models ----------
class BulkRequestItem(BaseModel):
    name: str
    direction: Optional[str] = "forward"
    fuzzy: Optional[bool] = True

class BulkRequest(BaseModel):
    requests: List[BulkRequestItem]

class PaymentInitRequest(BaseModel):
    name_count: int
    tier: str  # "single" or "bulk"
    request_payload: dict

# ---------- API Key Verification (for enterprise) ----------
async def verify_api_key(x_api_key: str = Header(...)):
    result = supabase.table("subscriptions").select("*").eq("api_key", x_api_key).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    sub = result.data[0]
    if datetime.fromisoformat(sub["current_period_end"]) < datetime.utcnow():
        raise HTTPException(status_code=403, detail="Subscription expired")
    return sub

# ---------- Rate limiter ----------
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)

# ---------- CORS ----------
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key"
    return response

@app.middleware("http")
async def cors_middleware(request, call_next):
    if request.method == "OPTIONS":
        response = JSONResponse(content={})
        return add_cors_headers(response)
    response = await call_next(request)
    return add_cors_headers(response)

# ---------- Search Helper (confidence removed) ----------
async def perform_search(payload: dict):
    if "requests" in payload:
        results = []
        for req in payload["requests"]:
            column = "old_name" if req["direction"] == "forward" else "new_name"
            query = supabase.table("name_changes").select("*")
            if req["fuzzy"]:
                query = query.ilike(column, f"%{req['name']}%")
            else:
                query = query.eq(column, req["name"])
            data = query.limit(20).execute().data
            for item in data:
                item.pop("confidence", None)
            results.append({
                "input_name": req["name"],
                "direction": req["direction"],
                "status": "found" if data else "not_found",
                "matches": data
            })
        return {
            "results": results,
            "summary": {
                "total": len(results),
                "found": sum(1 for r in results if r["status"] == "found"),
                "not_found": sum(1 for r in results if r["status"] == "not_found")
            }
        }
    else:
        name = payload["name"]
        direction = payload.get("direction", "forward")
        fuzzy = payload.get("fuzzy", True)
        column = "old_name" if direction == "forward" else "new_name"
        query = supabase.table("name_changes").select("*")
        if fuzzy:
            query = query.ilike(column, f"%{name}%")
        else:
            query = query.eq(column, name)
        data = query.limit(20).execute().data
        for item in data:
            item.pop("confidence", None)
        return {"status": "found" if data else "not_found", "data": data}

# ---------- Telegram Alert ----------
async def send_telegram_alert(transaction_id: str, amount: int, name_count: int, tier: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("Telegram credentials missing. Skipping alert.")
        return
    admin_link = "https://namecheck.co.ke/admin/payments"
    message = f"""🚨 *New Payment Pending Verification!*
    
Transaction ID: `{transaction_id}`
Amount: KES {amount}
Names: {name_count}
Tier: {tier}

👉 [Go to Admin Panel]({admin_link}) to mark as paid.
"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
            print(f"Telegram alert sent for {transaction_id}")
        except Exception as e:
            print(f"Telegram alert failed: {e}")

# ---------- Payment Endpoints ----------
@app.post("/payment/initiate")
async def initiate_payment(req: PaymentInitRequest):
    tx_id = f"NC{int(datetime.now().timestamp())}{random.randint(100,999)}"
    if req.tier == "single":
        amount = req.name_count * PRICES[PlanTier.SINGLE]
    elif req.tier == "bulk":
        amount = calculate_bulk_amount(req.name_count)
    else:
        raise HTTPException(status_code=400, detail="Invalid tier")
    
    paybill = "400200"
    account_number = "01101252731001"
    
    payment_data = {
        "transaction_id": tx_id,
        "amount": amount,
        "name_count": req.name_count,
        "tier": req.tier,
        "request_payload": req.request_payload,
        "status": "pending"
    }
    result = supabase.table("payments").insert(payment_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create payment record")
    
    return {
        "transaction_id": tx_id,
        "amount": amount,
        "paybill": paybill,
        "account_number": account_number
    }

@app.post("/payment/notify-admin")
async def notify_admin(transaction_id: str):
    result = supabase.table("payments").select("transaction_id, amount, name_count, tier, created_at").eq("transaction_id", transaction_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Transaction not found")
    payment = result.data[0]
    await send_telegram_alert(
        transaction_id=payment["transaction_id"],
        amount=payment["amount"],
        name_count=payment["name_count"],
        tier=payment["tier"]
    )
    return {"status": "notified"}

@app.post("/payment/mark-paid")
async def mark_paid(transaction_id: str, admin_token: str = Header(...)):
    if admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    update_data = {"status": "paid", "paid_at": datetime.utcnow().isoformat()}
    result = supabase.table("payments").update(update_data).eq("transaction_id", transaction_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {"status": "ok"}

@app.get("/payment/status/{transaction_id}")
async def get_payment_status(transaction_id: str):
    result = supabase.table("payments").select("status, request_payload, results_cache").eq("transaction_id", transaction_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Transaction not found")
    payment = result.data[0]
    if payment["status"] == "paid":
        if payment.get("results_cache"):
            results = payment["results_cache"]
        else:
            payload = payment["request_payload"]
            results = await perform_search(payload)
            supabase.table("payments").update({"results_cache": results}).eq("transaction_id", transaction_id).execute()
        return {"status": "paid", "results": results}
    return {"status": payment["status"], "results": None}

# ---------- Enterprise API Endpoint (with usage tracking) ----------
@app.post("/verify/bulk")
async def verify_bulk_enterprise(
    bulk_req: BulkRequest,
    request: Request,
    subscription: dict = Depends(verify_api_key)
):
    api_key = request.headers.get("x-api-key")
    search_count = len(bulk_req.requests)
    
    new_usage = subscription["searches_used_this_period"] + search_count
    overage = max(0, new_usage - subscription["included_searches"])
    
    supabase.table("subscriptions").update({"searches_used_this_period": new_usage}).eq("api_key", api_key).execute()
    
    if overage > 0:
        supabase.table("usage_log").insert({
            "api_key": api_key,
            "timestamp": datetime.utcnow().isoformat(),
            "query_count": search_count,
            "overage_count": overage,
            "cost_kes": overage * subscription["overage_rate"]
        }).execute()
    
    payload = {"requests": [req.dict() for req in bulk_req.requests]}
    results = await perform_search(payload)
    return results

# ---------- Enterprise Usage Endpoint ----------
@app.get("/enterprise/usage")
async def get_usage(subscription: dict = Depends(verify_api_key)):
    remaining = subscription["included_searches"] - subscription["searches_used_this_period"]
    if remaining < 0:
        remaining = 0
    return {
        "api_key": subscription["api_key"],
        "plan": subscription["plan"],
        "period_start": subscription["current_period_start"],
        "period_end": subscription["current_period_end"],
        "searches_used": subscription["searches_used_this_period"],
        "included_searches": subscription["included_searches"],
        "remaining_searches": remaining,
        "overage_rate": subscription["overage_rate"]
    }

# ---------- Public Endpoints ----------
@app.get("/verify")
async def verify_name(
    name: str = Query(..., min_length=3),
    direction: str = Query("forward", pattern="^(forward|backward)$"),
    fuzzy: bool = Query(True)
):
    column = "old_name" if direction == "forward" else "new_name"
    query = supabase.table("name_changes").select("*")
    if fuzzy:
        query = query.ilike(column, f"%{name}%")
    else:
        query = query.eq(column, name)
    result = query.limit(20).execute().data
    for item in result:
        item.pop("confidence", None)
    return {"status": "found" if result else "not_found", "data": result}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ---------- Admin Endpoints for API Key Management ----------
@app.post("/admin/api-keys")
async def create_api_key(
    admin_token: str = Header(...),
    plan: str = "starter",
    organization: str = None
):
    if admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    
    plan_config = get_enterprise_plan(plan)
    if not plan_config:
        raise HTTPException(status_code=400, detail="Invalid plan")
    
    api_key = str(uuid.uuid4())
    now = datetime.utcnow()
    period_end = now + timedelta(days=30)
    
    sub_data = {
        "api_key": api_key,
        "plan": plan,
        "monthly_fee": plan_config["monthly_fee"],
        "included_searches": plan_config["included_searches"],
        "overage_rate": plan_config["overage_rate"],
        "current_period_start": now.isoformat(),
        "current_period_end": period_end.isoformat(),
        "searches_used_this_period": 0,
        "organization": organization
    }
    result = supabase.table("subscriptions").insert(sub_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create API key")
    
    return {"api_key": api_key, "plan": plan, "valid_until": period_end.isoformat()}

@app.get("/admin/api-keys")
async def list_api_keys(admin_token: str = Header(...)):
    if admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    result = supabase.table("subscriptions").select("*").execute()
    return result.data

# ---------- Enterprise Request Endpoint ----------
@app.post("/api/enterprise-request")
async def enterprise_request(request: Request):
    data = await request.json()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        message = f"📋 *New Enterprise Request*\n\nOrganization: {data.get('organization')}\nEmail: {data.get('email')}\nPlan: {data.get('plan')}\nMessage: {data.get('message')}"
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})
    return {"status": "received"}

@app.get("/debug/routes")
async def debug_routes():
    routes = []
    for route in app.routes:
        routes.append({"path": route.path, "methods": list(route.methods)})
    return routes