"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /, /login
- API JSON             : /api/...
"""
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import auth, config, db
from app.auth import AccesRefuse, NonConnecte, exiger_admin, utilisateur_courant
from app.snmp_client import SnmpError, lire_infos_systeme
from app.utilisateurs import Utilisateur

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Supervision SNMP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Session dans un cookie signé (HttpOnly par défaut) : contient uniquement le login
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="supervision_session",
    max_age=config.SESSION_DUREE,
    same_site="lax",
)
app.add_exception_handler(NonConnecte, auth.gerer_non_connecte)
app.add_exception_handler(AccesRefuse, auth.gerer_acces_refuse)
app.include_router(auth.router)

Connecte = Annotated[Utilisateur, Depends(utilisateur_courant)]
Admin = Annotated[Utilisateur, Depends(exiger_admin)]


class NouvelEquipement(BaseModel):
    """Corps attendu par POST /api/equipements."""
    nom: str
    adresse: str
    communaute_snmp: str = "public"
    description: str | None = None


async def _interroger_cible() -> tuple[dict | None, str | None]:
    """Interroge l'équipement de test ; renvoie (infos, erreur)."""
    try:
        infos = await lire_infos_systeme(
            config.SNMP_TARGET, config.SNMP_COMMUNITY, config.SNMP_PORT,
            timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
        )
        return infos, None
    except SnmpError as exc:
        return None, str(exc)


# ---------------------------------------------------------------- pages HTML
@app.get("/", response_class=HTMLResponse)
async def accueil(request: Request, utilisateur: Connecte):
    equipements = db.lister_equipements()
    return templates.TemplateResponse(request, "index.html", {
        "utilisateur": utilisateur,
        "equipements": equipements,
    })


# ---------------------------------------------------------------- API JSON
@app.get("/api/sante")
async def sante():
    """Vérifie simplement que l'application répond (public, sans connexion)."""
    return {"statut": "ok", "version": app.version}


@app.get("/api/equipement-test")
async def equipement_test(utilisateur: Connecte):
    """Groupe "system" de l'équipement de test, au format JSON (démo sans base)."""
    infos, erreur = await _interroger_cible()
    if erreur:
        return JSONResponse(status_code=504, content={"cible": config.SNMP_TARGET, "erreur": erreur})
    return {"cible": config.SNMP_TARGET, **infos}


@app.get("/api/equipements")
async def lister_equipements_api(utilisateur: Connecte):
    """Liste des équipements enregistrés."""
    return [dict(ligne) for ligne in db.lister_equipements()]


@app.post("/api/equipements", status_code=201)
async def creer_equipement_api(payload: NouvelEquipement, admin: Admin):
    """Ajoute un équipement, après un test de connectivité SNMP (lecture de sysDescr)."""
    try:
        infos = await lire_infos_systeme(
            payload.adresse, payload.communaute_snmp,
            timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
        )
    except SnmpError as exc:
        return JSONResponse(status_code=400, content={"erreur": str(exc)})

    try:
        id_equipement = db.creer_equipement(
            nom=payload.nom, adresse=payload.adresse, communaute_snmp=payload.communaute_snmp,
            description=payload.description, description_systeme=infos["sysDescr"],
        )
    except sqlite3.IntegrityError:
        return JSONResponse(
            status_code=409, content={"erreur": f"Un équipement existe déjà pour « {payload.adresse} »"},
        )
    return {"id": id_equipement}
