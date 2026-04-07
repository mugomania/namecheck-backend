import os
from fastapi import FastAPI, Query
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

@app.get("/verify")
async def verify_name(
    name: str = Query(..., min_length=3),
    direction: str = Query("forward", regex="^(forward|backward)$"),
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
