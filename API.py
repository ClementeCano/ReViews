from datetime import datetime, timezone
from typing import List, Optional

import cloudinary
import cloudinary.uploader
import motor.motor_asyncio as motor
import requests
from bson import ObjectId
from environs import Env
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.middleware.sessions import SessionMiddleware

env = Env()
env.read_env()

MONGO_URI = env("MONGO_URI")
CLIENT_ID = env("CLIENT_ID")

cloudinary.config(
    cloud_name=env("CLOUDINARY_CLOUD_NAME"),
    api_key=env("CLOUDINARY_API_KEY"),
    api_secret=env("CLOUDINARY_API_SECRET"),
    secure=True,
)

# --- Base de datos ---
client = motor.AsyncIOMotorClient(MONGO_URI)

db = client["Parcial2"]
reviews_collection = db["Resenas"]

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key="SUPER_SECRET_KEY_RANDOM")

templates = Jinja2Templates(directory="templates")

# --- Utilidades ---
def parse_session_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    return None


def get_session_user(request: Request):
    token_exp = parse_session_datetime(request.session.get("token_exp"))
    if token_exp and datetime.now(timezone.utc) >= token_exp:
        request.session.clear()
        return None
    return request.session.get("user")


def require_user(request: Request):
    user = request.session.get("user")
    token = request.session.get("token")
    token_exp = parse_session_datetime(request.session.get("token_exp"))

    if not user or not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    
    if token_exp and datetime.now(timezone.utc) >= token_exp:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Token OAuth caducado")

    return {
        **user,
        "token": token,
        "iat": parse_session_datetime(request.session.get("token_iat")),
        "exp": token_exp,
    }


def geocode_address(address: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "ReViews/1.0"}
    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    if not data:
        return None, None

    return float(data[0]["lat"]), float(data[0]["lon"])


def serialize_review(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id")),
        "name": doc.get("name"),
        "address": doc.get("address"),
        "latitude": doc.get("latitude"),
        "longitude": doc.get("longitude"),
        "rating": doc.get("rating"),
        "author_email": doc.get("author_email"),
        "author_name": doc.get("author_name"),
        "token": doc.get("token"),
        "token_issued_at": doc.get("token_issued_at"),
        "token_expires_at": doc.get("token_expires_at"),
        "images": doc.get("images", []),
        "created_at": doc.get("created_at"),
    }


def parse_token_times(id_info: dict):
    issued = id_info.get("iat")
    expires = id_info.get("exp")
    issued_dt = (
        datetime.fromtimestamp(issued, tz=timezone.utc) if issued is not None else None
    )
    expires_dt = (
        datetime.fromtimestamp(expires, tz=timezone.utc) if expires is not None else None
    )
    return issued_dt, expires_dt


def upload_images(files: Optional[List[UploadFile]]) -> List[str]:
    if not files:
        return []

    uploaded_urls: List[str] = []
    for file in files:
        if not file or not file.filename:
            continue
        result = cloudinary.uploader.upload(
            file.file,
            folder="reviews",
            resource_type="auto",
        )
        secure_url = result.get("secure_url")
        if secure_url:
            uploaded_urls.append(secure_url)
    return uploaded_urls


@app.post("/login")
async def login(data: dict, request: Request):
    token = data.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Token requerido")
    try:
        id_info = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            CLIENT_ID
        )
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=401, detail="Token inválido") from exc

    issued_dt, expires_dt = parse_token_times(id_info)
    user_info = {
        "google_id": id_info.get("sub"),
        "email": id_info.get("email"),
        "name": id_info.get("name"),
        "picture": id_info.get("picture"),
    }

    request.session["user"] = user_info
    request.session["token"] = token
    request.session["token_iat"] = issued_dt.isoformat() if issued_dt else None
    request.session["token_exp"] = expires_dt.isoformat() if expires_dt else None
        
    return RedirectResponse(url='/reviews', status_code=303)

    

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

# --- Vistas ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: dict = Depends(get_session_user)):
    if user:
        return RedirectResponse(url="/reviews", status_code=303)
    return templates.TemplateResponse(
        "index.html", {"request": request, "client_id": CLIENT_ID, "user": user}
    )


@app.get("/reviews", response_class=HTMLResponse)
async def list_reviews(
    request: Request,
    selected: Optional[str] = None,
    user: dict = Depends(require_user),
):
    reviews: List[dict] = []
    cursor = reviews_collection.find().sort("created_at", -1)
    async for doc in cursor:
        reviews.append(serialize_review(doc))

    selected_review = None
    if selected:
        selected_review = next((r for r in reviews if r["id"] == selected), None)
        if not selected_review:
            try:
                db_review = await reviews_collection.find_one({"_id": ObjectId(selected)})
                if db_review:
                    selected_review = serialize_review(db_review)
            except Exception:
                selected_review = None

    return templates.TemplateResponse(
        "mapa.html",
        {
            "request": request,
            "user": user,
            "reviews": reviews,
            "selected_review": selected_review,
        },
    )


@app.post("/reviews", response_class=RedirectResponse)
async def create_review(
    name: str = Form(...),
    address: str = Form(...),
    rating: int = Form(...),
    images: Optional[List[UploadFile]] = File(None),
    user: dict = Depends(require_user),
):
    if rating < 0 or rating > 5:
        raise HTTPException(status_code=400, detail="La valoración debe estar entre 0 y 5")

    lat, lon = geocode_address(address)
    
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="No se pudo geocodificar la dirección")

    uploaded_urls = upload_images(images)

    review_doc = {
        "name": name,
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "rating": rating,
        "author_email": user["email"],
        "author_name": user.get("name"),
        "token": user.get("token"),
        "token_issued_at": user.get("iat"),
        "token_expires_at": user.get("exp"),
        "images": uploaded_urls,
        "created_at": datetime.now(timezone.utc),
    }
    
    result = await reviews_collection.insert_one(review_doc)
    review_id = str(result.inserted_id)

    return RedirectResponse(url=f"/reviews?selected={review_id}", status_code=303)


@app.get("/reviews/{review_id}")
async def get_review(review_id: str, user: dict = Depends(require_user)):
    try:
         db_review = await reviews_collection.find_one({"_id": ObjectId(review_id)})
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=400, detail="Identificador inválido") from exc

    if not db_review:
        raise HTTPException(status_code=404, detail="Reseña no encontrada")

    return serialize_review(db_review)