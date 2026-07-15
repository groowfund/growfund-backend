# ═══════════════════════════════════════════════════════════════
#  GrowFund TON — Backend API
#  Stack: FastAPI + MongoDB (Motor async)
#  Autor: GrowFund TON Team
#  Versión: 1.0.0
#  Despliegue recomendado: Railway.app (gratis)
# ═══════════════════════════════════════════════════════════════

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime, timedelta
import os, math, asyncio, logging

# ───────────────────────────────────────
#  CONFIG
# ───────────────────────────────────────
MONGO_URL       = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME         = os.getenv("DB_NAME", "growfund")
PLATFORM_ADDR   = "UQD-Hh6Bb_hn3k0zrizn_e32-5dWPThZnCDT0qhOtwMxTkHc"
DIAMOND_RATIO   = 5          # 1 TON = 5 diamantes
GFT_RATIO       = 0.2        # 1 diamante = 0.2 GFT (futuro)
PLATFORM_FEE    = 0.10       # 10% a la plataforma
WHALE_THRESHOLD = 500        # TON para ser Ballena
VOTE_THRESHOLD  = 0.66       # 66% de votos positivos para aprobar
VOTE_DAYS       = 30         # días hábiles de votación

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("growfund")

# ───────────────────────────────────────
#  FASTAPI APP
# ───────────────────────────────────────
app = FastAPI(
    title="GrowFund TON API",
    description="API de growfunding descentralizado sobre TON blockchain",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # En producción, limitá a tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────
#  DB CONNECTION
# ───────────────────────────────────────
client: AsyncIOMotorClient = None
db = None

@app.on_event("startup")
async def startup():
    global client, db
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    # Crear índices
    await db.users.create_index("wallet_address", unique=True)
    await db.projects.create_index("id", unique=True)
    await db.projects.create_index("status")
    await db.donations.create_index("wallet_address")
    await db.donations.create_index("project_id")
    await db.votes.create_index([("wallet_address", 1), ("project_id", 1)], unique=True)
    await db.interactions.create_index([("wallet_address", 1), ("project_id", 1)])
    logger.info("✅ Conectado a MongoDB")

@app.on_event("shutdown")
async def shutdown():
    if client:
        client.close()

def get_db():
    return db

# ───────────────────────────────────────
#  MODELOS PYDANTIC
# ───────────────────────────────────────

class UserCreate(BaseModel):
    wallet_address: str
    telegram_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_name: Optional[str] = None

class UserUpdate(BaseModel):
    telegram_username: Optional[str] = None
    telegram_name: Optional[str] = None

class User(BaseModel):
    wallet_address: str
    telegram_id: Optional[int] = None
    telegram_username: Optional[str] = None
    telegram_name: Optional[str] = None
    diamonds: float = 0.0
    ton_donated: float = 0.0
    roles: List[str] = []
    referral_code: Optional[str] = None
    referred_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=100)
    category: str
    description: str = Field(..., min_length=100)
    goal_amount: float = Field(..., gt=10)
    wallet_address: str           # wallet del emprendedor
    creator_address: str          # wallet del que propone (puede ser la misma)
    funding_days: int = Field(default=30, ge=15, le=90)

    @validator('wallet_address')
    def validate_ton_address(cls, v):
        if not (v.startswith('UQ') or v.startswith('EQ')):
            raise ValueError('Dirección TON inválida. Debe empezar con UQ o EQ.')
        return v

class Project(BaseModel):
    id: str
    name: str
    category: str
    description: str
    goal_amount: float
    raised_amount: float = 0.0
    wallet_address: str
    creator_address: str
    status: str = "votacion"  # votacion | activo | aprobado | exitoso | rechazado
    donors: List[str] = []
    donor_count: int = 0
    votes_yes: int = 0
    votes_no: int = 0
    vote_deadline: Optional[datetime] = None
    funding_days: int = 30
    likes: int = 0
    shares: int = 0
    comments_count: int = 0
    valuation: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class DonationCreate(BaseModel):
    wallet_address: str
    project_id: str
    amount_ton: float = Field(..., gt=0.05)
    tx_hash: Optional[str] = None   # Hash de la transacción en TON blockchain

class Donation(BaseModel):
    wallet_address: str
    project_id: str
    amount_ton: float
    diamonds_earned: float
    tx_hash: Optional[str] = None
    verified_on_chain: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

class VoteCreate(BaseModel):
    wallet_address: str
    project_id: str
    approve: bool

class InteractionCreate(BaseModel):
    wallet_address: str
    project_id: str
    action: str    # like | share | comment
    comment_text: Optional[str] = None

class CommentCreate(BaseModel):
    wallet_address: str
    project_id: str
    text: str = Field(..., min_length=3, max_length=500)

class EventCreate(BaseModel):
    event: str
    wallet_address: Optional[str] = None
    amount: Optional[float] = None
    tx_hash: Optional[str] = None
    project_id: Optional[str] = None
    timestamp: Optional[int] = None

# ───────────────────────────────────────
#  HELPERS
# ───────────────────────────────────────

def calc_diamonds(ton_amount: float) -> float:
    return round(ton_amount * DIAMOND_RATIO, 4)

def calc_valuation(likes: int, shares: int, comments: int) -> float:
    """Valuación basada en interacciones de la comunidad."""
    return (likes * 1) + (shares * 5) + (comments * 3)

def get_user_roles(ton_donated: float, has_projects: bool) -> List[str]:
    roles = []
    if ton_donated > 0:
        roles.append("donador")
    if has_projects:
        roles.append("emprendedor")
    if ton_donated >= WHALE_THRESHOLD:
        roles.append("ballena")
    return roles

def make_referral_code(wallet: str) -> str:
    return "GFT-" + wallet[2:6].upper()

async def check_vote_deadline(project_id: str, db):
    """Verifica si terminó el período de votación y actualiza el status."""
    project = await db.projects.find_one({"id": project_id})
    if not project:
        return
    if project.get("status") != "votacion":
        return

    deadline = project.get("vote_deadline")
    if deadline and datetime.utcnow() >= deadline:
        total = project["votes_yes"] + project["votes_no"]
        if total == 0:
            new_status = "rechazado"
        else:
            approval_pct = project["votes_yes"] / total
            new_status = "activo" if approval_pct >= VOTE_THRESHOLD else "rechazado"

        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
        )
        logger.info(f"Proyecto {project_id} → {new_status} ({project['votes_yes']}/{total} votos)")

# ───────────────────────────────────────
#  ENDPOINTS
# ───────────────────────────────────────

# ── HEALTH ──
@app.get("/", tags=["Sistema"])
async def root():
    return {
        "app": "GrowFund TON API",
        "version": "1.0.0",
        "status": "running",
        "platform_address": PLATFORM_ADDR,
        "diamond_ratio": DIAMOND_RATIO,
        "platform_fee_pct": PLATFORM_FEE * 100
    }

@app.get("/health", tags=["Sistema"])
async def health():
    try:
        await db.command("ping")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(503, f"DB error: {e}")

# ── USUARIOS ──

@app.post("/users", tags=["Usuarios"], response_model=dict)
async def create_or_get_user(user_data: UserCreate, db=Depends(get_db)):
    existing = await db.users.find_one({"wallet_address": user_data.wallet_address})
    if existing:
        existing.pop("_id", None)
        return {"created": False, "user": existing}

    # Crear usuario nuevo
    has_projects = False
    user = {
        "wallet_address": user_data.wallet_address,
        "telegram_id": user_data.telegram_id,
        "telegram_username": user_data.telegram_username,
        "telegram_name": user_data.telegram_name,
        "diamonds": 0.0,
        "ton_donated": 0.0,
        "roles": [],
        "referral_code": make_referral_code(user_data.wallet_address),
        "referred_by": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    await db.users.insert_one(user)
    user.pop("_id", None)
    return {"created": True, "user": user}

@app.get("/users/{wallet_address}", tags=["Usuarios"])
async def get_user(wallet_address: str, db=Depends(get_db)):
    user = await db.users.find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    user.pop("_id", None)
    return user

@app.get("/users/{wallet_address}/stats", tags=["Usuarios"])
async def get_user_stats(wallet_address: str, db=Depends(get_db)):
    user = await db.users.find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(404, "Usuario no encontrado")

    # Estadísticas completas
    donation_count = await db.donations.count_documents({"wallet_address": wallet_address})
    vote_count = await db.votes.count_documents({"wallet_address": wallet_address})
    project_count = await db.projects.count_documents({"creator_address": wallet_address})
    referral_count = await db.users.count_documents({"referred_by": wallet_address})

    has_projects = project_count > 0
    roles = get_user_roles(user["ton_donated"], has_projects)

    return {
        "wallet_address": wallet_address,
        "diamonds": user["diamonds"],
        "gft_estimated": round(user["diamonds"] * GFT_RATIO, 4),
        "ton_donated": user["ton_donated"],
        "roles": roles,
        "donation_count": donation_count,
        "vote_count": vote_count,
        "project_count": project_count,
        "referral_count": referral_count,
        "referral_code": user.get("referral_code"),
        "is_whale": user["ton_donated"] >= WHALE_THRESHOLD,
    }

# ── PROYECTOS ──

@app.post("/projects", tags=["Proyectos"], response_model=dict)
async def create_project(project: ProjectCreate, db=Depends(get_db)):
    # Verificar que el creador es donador
    user = await db.users.find_one({"wallet_address": project.creator_address})
    if not user:
        raise HTTPException(403, "Debés conectar tu billetera primero")

    import uuid
    project_id = str(uuid.uuid4())[:8].upper()

    # Calcular deadline de votación (30 días hábiles ≈ 42 días calendario)
    vote_deadline = datetime.utcnow() + timedelta(days=VOTE_DAYS * 1.4)

    doc = {
        "id": project_id,
        "name": project.name,
        "category": project.category,
        "description": project.description,
        "goal_amount": project.goal_amount,
        "raised_amount": 0.0,
        "wallet_address": project.wallet_address,
        "creator_address": project.creator_address,
        "status": "votacion",
        "donors": [],
        "donor_count": 0,
        "votes_yes": 0,
        "votes_no": 0,
        "vote_deadline": vote_deadline,
        "funding_days": project.funding_days,
        "likes": 0, "shares": 0, "comments_count": 0,
        "valuation": 0.0,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    await db.projects.insert_one(doc)
    doc.pop("_id", None)

    # Actualizar rol del usuario
    await db.users.update_one(
        {"wallet_address": project.creator_address},
        {"$addToSet": {"roles": "emprendedor"}}
    )

    logger.info(f"Proyecto creado: {project_id} - {project.name}")
    return {"project_id": project_id, "project": doc, "vote_deadline": vote_deadline.isoformat()}

@app.get("/projects", tags=["Proyectos"])
async def list_projects(
    status: Optional[str] = Query(None, description="votacion|activo|exitoso|rechazado"),
    category: Optional[str] = None,
    limit: int = Query(20, le=50),
    skip: int = 0,
    db=Depends(get_db)
):
    query = {}
    if status:
        query["status"] = status
    if category:
        query["category"] = {"$regex": category, "$options": "i"}

    cursor = db.projects.find(query).skip(skip).limit(limit).sort("created_at", -1)
    projects = []
    async for p in cursor:
        p.pop("_id", None)
        # Verificar deadline de votación en segundo plano
        await check_vote_deadline(p["id"], db)
        projects.append(p)

    total = await db.projects.count_documents(query)
    return {"total": total, "projects": projects}

@app.get("/projects/{project_id}", tags=["Proyectos"])
async def get_project(project_id: str, db=Depends(get_db)):
    await check_vote_deadline(project_id, db)
    p = await db.projects.find_one({"id": project_id})
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")
    p.pop("_id", None)
    return p

@app.get("/projects/{project_id}/valuation", tags=["Proyectos"])
async def get_project_valuation(project_id: str, db=Depends(get_db)):
    """Valuación en tiempo real basada en interacciones de la comunidad."""
    p = await db.projects.find_one({"id": project_id})
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")

    valuation = calc_valuation(p["likes"], p["shares"], p["comments_count"])
    roi_pct = 0.0
    if p["raised_amount"] > 0:
        roi_pct = round((valuation / max(p["raised_amount"], 1)) * 100, 2)

    return {
        "project_id": project_id,
        "likes": p["likes"],
        "shares": p["shares"],
        "comments": p["comments_count"],
        "valuation_score": valuation,
        "raised_amount": p["raised_amount"],
        "roi_estimate_pct": roi_pct,
        "formula": "likes×1 + shares×5 + comentarios×3"
    }

# ── DONACIONES ──

@app.post("/donations", tags=["Donaciones"], response_model=dict)
async def create_donation(donation: DonationCreate, background: BackgroundTasks, db=Depends(get_db)):
    # Verificar proyecto
    project = await db.projects.find_one({"id": donation.project_id})
    if not project:
        raise HTTPException(404, "Proyecto no encontrado")
    if project["status"] not in ("activo", "votacion"):
        raise HTTPException(400, "El proyecto no está activo para recibir donaciones")

    # Verificar/crear usuario
    user = await db.users.find_one({"wallet_address": donation.wallet_address})
    if not user:
        raise HTTPException(404, "Usuario no registrado. Conectá tu billetera primero.")

    # Calcular diamantes
    diamonds = calc_diamonds(donation.amount_ton)
    platform_cut = round(donation.amount_ton * PLATFORM_FEE, 6)
    entrepreneur_cut = round(donation.amount_ton - platform_cut, 6)

    # Guardar donación
    don_doc = {
        "wallet_address": donation.wallet_address,
        "project_id": donation.project_id,
        "amount_ton": donation.amount_ton,
        "diamonds_earned": diamonds,
        "platform_cut": platform_cut,
        "entrepreneur_cut": entrepreneur_cut,
        "tx_hash": donation.tx_hash,
        "verified_on_chain": donation.tx_hash is not None,
        "created_at": datetime.utcnow(),
    }
    await db.donations.insert_one(don_doc)

    # Actualizar usuario
    new_ton = user["ton_donated"] + donation.amount_ton
    new_diamonds = user["diamonds"] + diamonds
    has_projects = await db.projects.count_documents({"creator_address": donation.wallet_address}) > 0
    new_roles = get_user_roles(new_ton, has_projects)

    await db.users.update_one(
        {"wallet_address": donation.wallet_address},
        {"$inc": {"ton_donated": donation.amount_ton, "diamonds": diamonds},
         "$addToSet": {"roles": {"$each": new_roles}},
         "$set": {"updated_at": datetime.utcnow()}}
    )

    # Actualizar proyecto
    is_new_donor = donation.wallet_address not in project.get("donors", [])
    update_data = {
        "$inc": {"raised_amount": donation.amount_ton},
        "$set": {"updated_at": datetime.utcnow()}
    }
    if is_new_donor:
        update_data["$addToSet"] = {"donors": donation.wallet_address}
        update_data["$inc"]["donor_count"] = 1

    await db.projects.update_one({"id": donation.project_id}, update_data)

    # Verificar si alcanzó la meta
    new_raised = project["raised_amount"] + donation.amount_ton
    if new_raised >= project["goal_amount"]:
        await db.projects.update_one(
            {"id": donation.project_id},
            {"$set": {"status": "exitoso"}}
        )

    # Verificar si es nueva ballena
    is_new_whale = user["ton_donated"] < WHALE_THRESHOLD and new_ton >= WHALE_THRESHOLD

    logger.info(f"Donación: {donation.wallet_address} → {donation.project_id}: {donation.amount_ton} TON, {diamonds} 💎")

    return {
        "success": True,
        "diamonds_earned": diamonds,
        "gft_estimated": round(diamonds * GFT_RATIO, 4),
        "platform_cut_ton": platform_cut,
        "entrepreneur_cut_ton": entrepreneur_cut,
        "total_diamonds": new_diamonds,
        "total_ton_donated": new_ton,
        "new_roles": new_roles,
        "new_whale": is_new_whale,
        "tx_hash": donation.tx_hash,
        "project_raised": new_raised,
    }

@app.get("/donations/{wallet_address}", tags=["Donaciones"])
async def get_user_donations(wallet_address: str, limit: int = 20, db=Depends(get_db)):
    cursor = db.donations.find(
        {"wallet_address": wallet_address},
        {"_id": 0}
    ).sort("created_at", -1).limit(limit)
    donations = [d async for d in cursor]
    total_ton = sum(d["amount_ton"] for d in donations)
    total_diamonds = sum(d["diamonds_earned"] for d in donations)
    return {
        "wallet_address": wallet_address,
        "donations": donations,
        "total_ton": total_ton,
        "total_diamonds": total_diamonds
    }

# ── VOTACIONES ──

@app.post("/votes", tags=["Votaciones"], response_model=dict)
async def cast_vote(vote: VoteCreate, background: BackgroundTasks, db=Depends(get_db)):
    # Verificar que el usuario es donador
    user = await db.users.find_one({"wallet_address": vote.wallet_address})
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    if "donador" not in user.get("roles", []):
        raise HTTPException(403, "Solo los donadores pueden votar")

    # Verificar proyecto
    project = await db.projects.find_one({"id": vote.project_id})
    if not project:
        raise HTTPException(404, "Proyecto no encontrado")
    if project["status"] != "votacion":
        raise HTTPException(400, "El proyecto no está en período de votación")

    deadline = project.get("vote_deadline")
    if deadline and datetime.utcnow() > deadline:
        raise HTTPException(400, "El período de votación ha finalizado")

    # Evitar voto duplicado
    existing_vote = await db.votes.find_one({
        "wallet_address": vote.wallet_address,
        "project_id": vote.project_id
    })
    if existing_vote:
        raise HTTPException(409, "Ya votaste en este proyecto")

    # Calcular peso del voto (Ballenas votan x3)
    is_whale = "ballena" in user.get("roles", [])
    vote_weight = 3 if is_whale else 1

    # Guardar voto
    await db.votes.insert_one({
        "wallet_address": vote.wallet_address,
        "project_id": vote.project_id,
        "approve": vote.approve,
        "weight": vote_weight,
        "is_whale": is_whale,
        "created_at": datetime.utcnow()
    })

    # Actualizar contadores del proyecto
    field = "votes_yes" if vote.approve else "votes_no"
    await db.projects.update_one(
        {"id": vote.project_id},
        {"$inc": {field: vote_weight}, "$set": {"updated_at": datetime.utcnow()}}
    )

    # Verificar si ya se puede resolver la votación
    updated = await db.projects.find_one({"id": vote.project_id})
    total_votes = updated["votes_yes"] + updated["votes_no"]
    approval_pct = updated["votes_yes"] / total_votes if total_votes > 0 else 0

    background.add_task(check_vote_deadline, vote.project_id, db)

    return {
        "success": True,
        "vote": "✅ A favor" if vote.approve else "❌ En contra",
        "whale_bonus": is_whale,
        "vote_weight": vote_weight,
        "current_approval_pct": round(approval_pct * 100, 1),
        "total_votes": total_votes,
        "threshold_needed": VOTE_THRESHOLD * 100
    }

@app.get("/votes/{project_id}", tags=["Votaciones"])
async def get_project_votes(project_id: str, db=Depends(get_db)):
    project = await db.projects.find_one({"id": project_id})
    if not project:
        raise HTTPException(404, "Proyecto no encontrado")

    total = project["votes_yes"] + project["votes_no"]
    approval_pct = (project["votes_yes"] / total * 100) if total > 0 else 0
    days_left = None
    if project.get("vote_deadline"):
        delta = project["vote_deadline"] - datetime.utcnow()
        days_left = max(0, delta.days)

    return {
        "project_id": project_id,
        "votes_yes": project["votes_yes"],
        "votes_no": project["votes_no"],
        "total_votes": total,
        "approval_pct": round(approval_pct, 1),
        "threshold_pct": VOTE_THRESHOLD * 100,
        "approved": approval_pct >= VOTE_THRESHOLD * 100,
        "status": project["status"],
        "vote_deadline": project.get("vote_deadline"),
        "days_remaining": days_left
    }

# ── INTERACCIONES (likes, shares, comentarios) ──

@app.post("/interactions", tags=["Interacciones"])
async def add_interaction(interaction: InteractionCreate, db=Depends(get_db)):
    valid_actions = {"like", "share", "comment"}
    if interaction.action not in valid_actions:
        raise HTTPException(400, f"Acción inválida. Debe ser: {valid_actions}")

    project = await db.projects.find_one({"id": interaction.project_id})
    if not project:
        raise HTTPException(404, "Proyecto no encontrado")

    # Para likes: evitar duplicados
    if interaction.action == "like":
        existing = await db.interactions.find_one({
            "wallet_address": interaction.wallet_address,
            "project_id": interaction.project_id,
            "action": "like"
        })
        if existing:
            # Toggle: quitar el like
            await db.interactions.delete_one({"_id": existing["_id"]})
            await db.projects.update_one(
                {"id": interaction.project_id},
                {"$inc": {"likes": -1}}
            )
            return {"action": "unlike", "message": "Like removido"}

    # Guardar interacción
    int_doc = {
        "wallet_address": interaction.wallet_address,
        "project_id": interaction.project_id,
        "action": interaction.action,
        "comment_text": interaction.comment_text if interaction.action == "comment" else None,
        "created_at": datetime.utcnow()
    }
    await db.interactions.insert_one(int_doc)

    # Actualizar contadores y valuación
    field_map = {"like": "likes", "share": "shares", "comment": "comments_count"}
    field = field_map[interaction.action]
    await db.projects.update_one(
        {"id": interaction.project_id},
        {"$inc": {field: 1}, "$set": {"updated_at": datetime.utcnow()}}
    )

    # Recalcular valuación
    updated = await db.projects.find_one({"id": interaction.project_id})
    new_valuation = calc_valuation(updated["likes"], updated["shares"], updated["comments_count"])
    await db.projects.update_one(
        {"id": interaction.project_id},
        {"$set": {"valuation": new_valuation}}
    )

    return {
        "success": True,
        "action": interaction.action,
        "new_valuation": new_valuation,
        "likes": updated["likes"],
        "shares": updated["shares"],
        "comments": updated["comments_count"]
    }

@app.get("/interactions/{project_id}/comments", tags=["Interacciones"])
async def get_comments(project_id: str, limit: int = 20, db=Depends(get_db)):
    cursor = db.interactions.find(
        {"project_id": project_id, "action": "comment"},
        {"_id": 0}
    ).sort("created_at", -1).limit(limit)
    comments = [c async for c in cursor]
    return {"project_id": project_id, "comments": comments, "total": len(comments)}

# ── EVENTOS (desde frontend) ──

@app.post("/events", tags=["Sistema"])
async def receive_event(event: EventCreate, db=Depends(get_db)):
    """Recibe eventos del frontend (transacciones, etc.) para registrar."""
    doc = {
        "event": event.event,
        "wallet_address": event.wallet_address,
        "amount": event.amount,
        "tx_hash": event.tx_hash,
        "project_id": event.project_id,
        "timestamp": event.timestamp,
        "received_at": datetime.utcnow()
    }
    await db.events.insert_one(doc)
    return {"received": True}

# ── ESTADÍSTICAS GLOBALES ──

@app.get("/stats", tags=["Sistema"])
async def global_stats(db=Depends(get_db)):
    total_users = await db.users.count_documents({})
    total_projects = await db.projects.count_documents({})
    active_projects = await db.projects.count_documents({"status": "activo"})
    voting_projects = await db.projects.count_documents({"status": "votacion"})
    total_donations = await db.donations.count_documents({})

    # Suma total recaudada
    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$amount_ton"}}}]
    result = await db.donations.aggregate(pipeline).to_list(1)
    total_ton = result[0]["total"] if result else 0.0

    total_diamonds = await db.users.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$diamonds"}}}
    ]).to_list(1)
    total_dm = total_diamonds[0]["total"] if total_diamonds else 0.0

    whales = await db.users.count_documents({"ton_donated": {"$gte": WHALE_THRESHOLD}})

    return {
        "total_users": total_users,
        "total_projects": total_projects,
        "active_projects": active_projects,
        "voting_projects": voting_projects,
        "total_donations": total_donations,
        "total_ton_raised": round(total_ton, 4),
        "total_diamonds_issued": round(total_dm, 2),
        "whale_count": whales,
        "platform_address": PLATFORM_ADDR,
        "diamond_ratio": DIAMOND_RATIO,
        "platform_fee_pct": PLATFORM_FEE * 100
    }

@app.get("/ranking", tags=["Sistema"])
async def get_ranking(limit: int = 20, by: str = Query("diamonds", description="diamonds|ton|votes"), db=Depends(get_db)):
    sort_field = {"diamonds": "diamonds", "ton": "ton_donated", "votes": "vote_count"}.get(by, "diamonds")
    cursor = db.users.find(
        {"ton_donated": {"$gt": 0}},
        {"_id": 0, "wallet_address": 1, "telegram_username": 1, "diamonds": 1, "ton_donated": 1, "roles": 1}
    ).sort(sort_field, -1).limit(limit)
    users = [u async for u in cursor]
    return {"ranking": users, "sorted_by": by}


# ── RUN (para desarrollo local) ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
