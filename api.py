import os
import uuid
import random
import httpx  # pip install httpx
from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from datetime import datetime

load_dotenv()

app = FastAPI()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

@app.get("/debug/routes")
async def debug_routes():
    routes = []
    for route in app.routes:
        routes.append({"path": route.path, "methods": list(route.methods)})
    return routes

# ---------- Bulk query models ----------
class BulkRequestItem(BaseModel):
    name: str
    direction: Optional[str] = "forward"
    fuzzy: Optional[bool] = True

class BulkRequest(BaseModel):
    requests: List[BulkRequestItem]

# ---------- Payment models ----------
class PaymentInitRequest(BaseModel):
    name_count: int
    is_bulk: bool = False
    request_payload: dict

# ---------- API key store (using environment variables for production) ----------
API_KEYS = {
    os.getenv("INSTITUTION_API_KEY_1"): {"tier": "basic", "rate_limit": 100},
    os.getenv("INSTITUTION_API_KEY_2"): {"tier": "professional", "rate_limit": 500},
}

def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return API_KEYS[x_api_key]

# ---------- Rate limiter ----------
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)

# ---------- Helper function for CORS ----------
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.middleware("http")
async def cors_middleware(request, call_next):
    if request.method == "OPTIONS":
        response = JSONResponse(content={})
        return add_cors_headers(response)
    response = await call_next(request)
    return add_cors_headers(response)

# ---------- Pricing helper ----------
def calculate_amount(name_count: int, is_bulk: bool) -> int:
    if not is_bulk:
        return name_count * 50
    if name_count <= 10:
        return name_count * 45
    elif name_count <= 50:
        return name_count * 40
    else:
        return name_count * 35

# ---------- Reusable search function ----------
async def perform_search(payload: dict):
    # Detect bulk by checking for "requests" key
    if "requests" in payload:
        # Bulk search
        requests = payload["requests"]
        results = []
        for req in requests:
            column = "old_name" if req["direction"] == "forward" else "new_name"
            query = supabase.table("name_changes").select("*")
            if req["fuzzy"]:
                query = query.ilike(column, f"%{req['name']}%")
            else:
                query = query.eq(column, req["name"])
            data = query.limit(20).execute().data
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
        # Single search
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
        return {"status": "found" if data else "not_found", "data": data}

# ---------- Telegram alert ----------
async def send_telegram_alert(transaction_id: str, amount: int, name_count: int, is_bulk: bool):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("Telegram credentials missing. Skipping alert.")
        return
    message = f"""🚨 *New Payment Pending Verification!*
    
Transaction ID: `{transaction_id}`
Amount: KES {amount}
Names: {name_count}
Bulk: {'Yes' if is_bulk else 'No'}

Please check M-Pesa statement and mark as paid in the admin panel.
"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
            print(f"Telegram alert sent for {transaction_id}")
        except Exception as e:
            print(f"Telegram alert failed: {e}")

# ---------- Payment endpoints ----------
@app.post("/payment/initiate")
async def initiate_payment(req: PaymentInitRequest):
    tx_id = f"NC{int(datetime.now().timestamp())}{random.randint(100,999)}"
    amount = calculate_amount(req.name_count, req.is_bulk)
    paybill = "400200"
    account_number = "01101252731001"
    
    payment_data = {
        "transaction_id": tx_id,
        "amount": amount,
        "name_count": req.name_count,
        "is_bulk": req.is_bulk,
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
    # Fetch payment details
    result = supabase.table("payments").select("transaction_id, amount, name_count, is_bulk, created_at").eq("transaction_id", transaction_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Transaction not found")
    payment = result.data[0]
    # Send Telegram alert
    await send_telegram_alert(
        transaction_id=payment["transaction_id"],
        amount=payment["amount"],
        name_count=payment["name_count"],
        is_bulk=payment["is_bulk"]
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

# ---------- Existing endpoints ----------
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
    result = query.limit(20).execute()
    return {"status": "found" if result.data else "not_found", "data": result.data}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/verify/bulk")
@limiter.limit("100/minute")
async def verify_bulk(
    bulk_req: BulkRequest,
    request: Request,
    api_key_info: dict = Depends(verify_api_key)
):
    results = []
    for req in bulk_req.requests:
        column = "old_name" if req.direction == "forward" else "new_name"
        query = supabase.table("name_changes").select("*")
        if req.fuzzy:
            query = query.ilike(column, f"%{req.name}%")
        else:
            query = query.eq(column, req.name)
        data = query.limit(20).execute().data
        results.append({
            "input_name": req.name,
            "direction": req.direction,
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
