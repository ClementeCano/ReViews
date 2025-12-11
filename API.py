from datetime import datetime
import os
from typing import Optional
from bson import ObjectId
from environs import Env
from fastapi import FastAPI, File, Form, Request, Depends, HTTPException, UploadFile, requests
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import motor.motor_asyncio as motor
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

# --- IMPORTAMOS LA LIBRERÍA DE VERIFICACIÓN DE GOOGLE ---
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from marcador import Marcador
from objeto1 import Objeto1
import requests

# Importar cloudinary
import cloudinary.uploader
import cloudinary

env = Env()
env.read_env()

# Variables de entorno
uri = env('MONGO_URI')
client_id = env('CLIENT_ID')
# client_secret ya no es necesario para este flujo, pero puedes dejarlo si quieres

# --- CONFIGURACIÓN DE BASE DE DATOS (Mantenemos tu código) ---
client = motor.AsyncIOMotorClient(uri)
db = client["Parcial2"]
coleccion1 = db["Tabla1"]
mapas_coleccion = db["Mapas"]
archivos_coleccion = db["Archivos"]
usuarios_coleccion = db["Usuarios"]
marcadores_coleccion = db["Marcadores"]
visitas_coleccion = db["Visitas"]

app = FastAPI()

# --- CONFIGURACIÓN DE MIDDLEWARE (IMPORTANTE) ---
# Clave secreta para firmar la cookie de sesión
app.add_middleware(SessionMiddleware, secret_key="SUPER_SECRET_KEY_RANDOM")

# Configurar Jinja2
templates = Jinja2Templates(directory="templates")

cloudinary.config( 
    cloud_name = env('CLOUDINARY_CLOUD_NAME'), 
    api_key = env('CLOUDINARY_API_KEY'), 
    api_secret = env('CLOUDINARY_API_SECRET'),
    secure = True
)


path = "/path"


# --- MODELO DE DATOS ---
# Para recibir el JSON que envía tu frontend: { "token": "..." }
class TokenData(BaseModel):
    token: str

# --- DEPENDENCIA ---
def get_user(request: Request):
    return request.session.get('user')

# --- RUTAS DE AUTENTICACIÓN (ADAPTADAS) ---

@app.post("/login")
async def login(data: TokenData, request: Request):
    """
    Recibe el token de Google desde el Frontend, lo verifica
    y crea la sesión de usuario.
    """
    try:
        # 1. Verificar el token con los servidores de Google
        id_info = id_token.verify_oauth2_token(
            data.token, 
            google_requests.Request(), 
            client_id
        )

        # 2. Si el token es válido, extraemos la info
        user_info = {
            "google_id": id_info.get("sub"),
            "email": id_info.get("email"),
            "name": id_info.get("name"),
            "picture": id_info.get("picture")
        }

        # 3. (OPCIONAL) Aquí podrías guardar o actualizar el usuario en tu MongoDB
        # await usuarios.update_one({"email": user_info["email"]}, {"$set": user_info}, upsert=True)

        # 4. Guardamos al usuario en la cookie de SESIÓN
        request.session['user'] = user_info
        request.session['token'] = data.token  # Guardamos el token si lo necesitamos luego
        
        return RedirectResponse(url='/mapa', status_code=303)

    except ValueError as e:
        # El token es falso, expiró o el client_id no coincide
        print(f"Error validando token: {e}")
        raise HTTPException(status_code=401, detail="Token inválido")

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    # Redirigimos al home tras cerrar sesión
    return RedirectResponse(url='/', status_code=303)

# --- VISTAS (FRONTEND) ---

@app.get("/")
async def home(request: Request, user: dict = Depends(get_user)):
    # Renderizamos el index.html. 
    # Es importante pasarle 'client_id' para que el botón de Google funcione.
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "user": user, 
        "client_id": client_id
    })

############## MAPAS Y MARCADORES ################
@app.get("/mapa")
async def ver_mapa(request: Request, email: Optional[str] = None, user: dict = Depends(get_user)):
    """
    Vista principal: Muestra el mapa con los marcadores del usuario.
    """
    if not user:
        return RedirectResponse(url='/', status_code=303)

    correo_objetivo = email or user["email"]
    puede_editar = correo_objetivo == user["email"]

    # 1. Recuperamos los marcadores de la BD usando la misma lógica que el endpoint
    marcadores_list = []
    cursor = marcadores_coleccion.find({"email_usuario": correo_objetivo})
    async for doc in cursor:
        # Convertimos a formato usable en JS: lat, lon, ciudad, imagen
        marcadores_list.append({
            "ciudad": doc["ciudad_pais"],
            "lat": doc["latitud"],
            "lon": doc["longitud"],
            "img": doc.get("imagen_url", "")
        })

    # 2. Registrar visita cuando el usuario consulta un mapa (propio o ajeno)
    await visitas_coleccion.insert_one({
        "email_visitado": correo_objetivo,
        "email_visitante": user["email"],
        "token_visitante": request.session.get("token", ""),
        "timestamp": datetime.utcnow()
    })

    # 3. Recuperar visitas recibidas por el mapa mostrado
    visitas = []
    visitas_cursor = visitas_coleccion.find({"email_visitado": correo_objetivo}).sort("timestamp", -1)
    async for visita in visitas_cursor:
        visitas.append({
            "email_visitante": visita.get("email_visitante", "Desconocido"),
            "token_visitante": visita.get("token_visitante", ""),
            "timestamp": visita.get("timestamp")
        })

    # 4. Renderizamos el HTML pasando la lista de marcadores y las visitas
    return templates.TemplateResponse("mapa.html", {
        "request": request,
        "email_usuario": correo_objetivo,
        "marcadores": marcadores_list,
        "puede_editar": puede_editar,
        "visitas": visitas,
        "email_actual": user["email"]
    })

@app.post("/marcadores", tags=["Marcadores"])
async def crear_marcador(marcador: Marcador):
    """
    Guarda un nuevo marcador.
    """
    # Convertimos el modelo a diccionario
    marcador_dict = marcador.model_dump()
    
    # Insertamos en Mongo
    await marcadores_coleccion.insert_one(marcador_dict)
    
    # Devolvemos el mismo objeto que recibimos (sin el _id de mongo)
    return {"mensaje": "Marcador guardado correctamente", "marcador": marcador}

@app.get("/marcadores/{email}", tags=["Marcadores"])
async def obtener_marcadores(email: str):
    """
    Devuelve todos los marcadores asociados a un email.
    """
    marcadores = []
    # Buscamos en Mongo filtrando por el email del usuario
    cursor = marcadores_coleccion.find({"email_usuario": email})
    
    async for doc in cursor:
        if "_id" in doc:
            del doc["_id"]  # <--- CORRECCIÓN: Eliminamos _id como en tu examen
        
        # Convertimos el dict al modelo Pydantic y lo añadimos a la lista
        marcadores.append(Marcador(**doc))
        
    return marcadores

# --- FUNCIÓN AUXILIAR PARA GEOCODING (OSM Nominatim) ---
def obtener_coordenadas(ciudad: str):
    """
    Usa la API gratuita de OpenStreetMap para obtener lat/lon.
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": ciudad,
        "format": "json",
        "limit": 1
    }
    # Es importante poner un User-Agent para que no nos bloqueen
    headers = {"User-Agent": "MiMapaExamen/1.0"}
    
    response = requests.get(url, params=params, headers=headers)
    data = response.json()
    
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None, None


# --- ENDPOINT PARA EL FORMULARIO HTML ---

@app.post("/web/nuevo-marcador", tags=["Vistas"])
async def crear_marcador_web(
    request: Request,
    email: str = Form(...),       # Viene del <input name="email"> (oculto)
    ciudad: str = Form(...),      # Viene del <input name="ciudad">
    imagen: UploadFile = File(...) # Viene del <input type="file" name="imagen">
):
    """
    Recibe el formulario, busca coordenadas, sube foto y guarda en Mongo.
    """
    
    # 1. GEOCODING: Obtener latitud y longitud
    lat, lon = obtener_coordenadas(ciudad)
    
    if lat is None or lon is None:
        # Si no encuentra la ciudad, podríamos devolver un error, 
        # pero para simplificar redirigimos con un print en consola
        print(f"Error: No se encontraron coordenadas para {ciudad}")
        return RedirectResponse(url=f"/mapa", status_code=303)


    # 2. CLOUDINARY: Subir la imagen
    url_imagen = ""
    if imagen.filename:
        # Subimos el archivo a Cloudinary
        resultado = cloudinary.uploader.upload(imagen.file, folder="archivos")  # Nombre de la carpeta en Cloudinary
        url_imagen = resultado.get("secure_url")

    # 3. GUARDAR EN MONGO (Reutilizamos lógica del modelo)
    nuevo_marcador = {
        "email_usuario": email,
        "ciudad_pais": ciudad,
        "latitud": lat,
        "longitud": lon,
        "imagen_url": url_imagen
    }
    
    await marcadores_coleccion.insert_one(nuevo_marcador)

    # 4. REDIRIGIR: Volvemos al mapa para ver el nuevo punto
    return RedirectResponse(url=f"/mapa?email={email}", status_code=303)

############### ENDPOINTS DEL FRONTEND ###############

# Obtener todos los objeto1
@app.get(path)
async def obtener_todos_Objeto1_con_id():
    lista_objeto1 = []
    async for objeto1 in coleccion1.find():  # eventos_collection es tu colección en MongoDB
        objeto1_dict = {
            "id": str(objeto1["_id"]),
            "lista1": objeto1.get("lista1", []),
            "descripcion": objeto1.get("descripcion", ""),
            "booleano": objeto1.get("booleano", False),
            "fecha": objeto1.get("fecha", None).isoformat() if objeto1.get("fecha", None) else None,
            "entero": objeto1.get("entero", 0) 
        }
        lista_objeto1.append(objeto1_dict)
        return lista_objeto1

# Obtener un objeto1 por su id
@app.get(path + "/{objeto1_id}")
async def obtener_Objeto1_por_id(objeto1_id: str):
    objeto1 = await coleccion1.find_one({"_id": ObjectId(objeto1_id)})
    if objeto1:
        objeto1_dict = {
            "id": str(objeto1["_id"]),
            "lista1": objeto1.get("lista1", []),
            "descripcion": objeto1.get("descripcion", ""),
            "booleano": objeto1.get("booleano", False),
            "fecha": objeto1.get("fecha", None).isoformat() if objeto1.get("fecha", None) else None,
            "entero": objeto1.get("entero", 0) 
        }
        return objeto1_dict
    else:
        raise HTTPException(status_code=404, detail="Objeto1 no encontrado")

# Crear un objeto1
@app.post(path)
async def crear_Objeto1(lista1: str = Form(...),  
                        descripcion: str = Form(...), 
                        booleano: bool = Form(...), 
                        fecha: str = Form(...), 
                        entero: int = Form(...)):
    try:
        fecha_dt = datetime.fromisoformat(fecha)
        
        nuevo_objeto1 = {
            "lista1": lista1.split(",") if lista1 else [],
            "descripcion": descripcion,
            "booleano": booleano,
            "fecha": fecha_dt,
            "entero": entero,
            }
        
        resultado = await coleccion1.insert_one(nuevo_objeto1)
        nuevo_id = resultado.inserted_id
        
        # Cambiar por Templates Jinja2 si es necesario
        #return Jinja2Templates.TemplateResponse("exito.html", {
        return {"mensaje": "Objeto1 creado correctamente", "id": str(nuevo_id)}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

@app.post(path + "/actualizar/{objeto1_id}")
async def actualizar_Objeto1(objeto1_id: str,
                             lista1: str = Form(...),
                            descripcion: str = Form(...), 
                            booleano: bool = Form(...), 
                            fecha: str = Form(...), 
                            entero: int = Form(...)):
    try:
        fecha_dt = datetime.fromisoformat(fecha)
        
        objeto1_actualizado = {
            "lista1": lista1.split(",") if lista1 else [],
            "descripcion": descripcion,
            "booleano": booleano,
            "fecha": fecha_dt,
            "entero": entero,
        }
        
        resultado = await coleccion1.update_one({"_id": ObjectId(objeto1_id)}, {"$set": objeto1_actualizado})
        
        # Cambiar por Templates Jinja2 si es necesario
        #return Jinja2Templates.TemplateResponse("exito.html", {
        return {"mensaje": "Objeto1 actualizado correctamente", "id": objeto1_id}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")

# Eliminar un objeto1 por su clave1
#BORRAR
@app.delete(path + "/{objeto1_id}")
async def eliminar_Objeto1(objeto1_id: str):
    try:
        id = ObjectId(objeto1_id)
        #print("ID a eliminar:", id)
    except:
        raise HTTPException(status_code=400, detail="ID de objeto1 inválido.")

    await coleccion1.delete_one({"_id": id})

    return {"mensaje": "Objeto1 eliminado correctamente"}

# Filtrar objeto1
@app.get(path + "/filtrar")
async def filtrar_objeto1(
    # Definimos los mismos parámetros que tiene el formulario HTML
    descripcion: Optional[str] = None,
    booleano: Optional[str] = None,
    start_fecha: Optional[str] = None,
    end_fecha: Optional[str] = None,
    entero: Optional[str] = None
) -> dict:
    filtros = {}
    
    if descripcion:
        filtros["descripcion"] = {"$regex": descripcion, "$options": "i"}
    
    if booleano:
        filtros["booleano"] = booleano
    
    if start_fecha and end_fecha:
        filtros["fecha"] = {"$gte": datetime.fromisoformat(start_fecha), "$lte": datetime.fromisoformat(end_fecha)}
    elif start_fecha:
        filtros["fecha"] = {"$gte": datetime.fromisoformat(start_fecha)}
    elif end_fecha:
        filtros["fecha"] = {"$lte": datetime.fromisoformat(end_fecha)}
    
    if entero:
        filtros["entero"] = entero


    # Debug: mostrar filtros recibidos
    print("[filtrar_objeto1] filtros:", filtros)

    lista_objeto1 = []
    cursor = coleccion1.find(filtros)
    async for reg in cursor:
        objeto1 = await obtener_Objeto1_por_id(str(reg["_id"]))
        lista_objeto1.append(objeto1)

    # Debug: mostrar cuántos documentos devuelve la consulta
    print(f"[filtrar_objeto1] encontrados: {len(lista_objeto1)}")

    return {"lista_objeto1": lista_objeto1}

# Funcion para subir imagen a cloudinary
async def upload_image(archivo_id: str, file: UploadFile):
    try:        
        # CORRECCIÓN: Usamos 'public_id' y 'folder'
        # Quitamos el try/except silencioso para ver si falla aquí
        upload_result = cloudinary.uploader.upload(
            file.file,
            public_id=str(archivo_id),  # <--- CORREGIDO: public_id
            folder="archivos", # Carpeta en Cloudinary
            resource_type="auto"
        )
        
        return upload_result["secure_url"]

    except Exception as e:
        # Imprimimos el error REAL en la consola para que lo veas
        print(f"ERROR CLOUDINARY: {e}")
        # Re-lanzamos el error para que el endpoint se entere y falle
        raise e