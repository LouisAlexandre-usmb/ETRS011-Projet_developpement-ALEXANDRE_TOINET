"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /
- API JSON             : /api/...
"""
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import config, db
from app.snmp_client import SnmpError, lire_infos_systeme

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Supervision SNMP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
async def accueil(request: Request):
    equipements = db.lister_equipements()
    return templates.TemplateResponse(request, "index.html", {
        "equipements": equipements,
    })


# ---------------------------------------------------------------- API JSON
@app.get("/api/sante")
async def sante():
    """Vérifie simplement que l'application répond."""
    return {"statut": "ok", "version": app.version}


@app.get("/api/equipement-test")
async def equipement_test():
    """Groupe "system" de l'équipement de test, au format JSON (démo sans base)."""
    infos, erreur = await _interroger_cible()
    if erreur:
        return JSONResponse(status_code=504, content={"cible": config.SNMP_TARGET, "erreur": erreur})
    return {"cible": config.SNMP_TARGET, **infos}


@app.get("/api/equipements")
async def lister_equipements_api():
    """Liste des équipements enregistrés."""
    return [dict(ligne) for ligne in db.lister_equipements()]


@app.post("/api/equipements", status_code=201)
async def creer_equipement_api(payload: NouvelEquipement):
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
