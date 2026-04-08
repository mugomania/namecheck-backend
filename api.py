import os
from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

load_dotenv()

app = FastAPI()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ---------- Bulk query models ----------
class BulkRequestItem(BaseModel):
    name: str
    direction: Optional[str] = "forward"
    fuzzy: Optional[bool] = True

class BulkRequest(BaseModel):
    requests: List[BulkRequestItem]

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

# ---------- New bulk endpoint ----------
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