"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /, /login
- API JSON             : /api/...
"""
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    groupe_id: int | None = None


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
async def tableau_de_bord(request: Request, utilisateur: Connecte):
    equipements = db.lister_equipements()
    return templates.TemplateResponse(request, "index.html", {
        "utilisateur": utilisateur,
        "equipements": equipements,
        "page_active": "tableau_de_bord",
    })


@app.get("/inventaire", response_class=HTMLResponse)
async def inventaire(request: Request, admin: Admin):
    equipements = db.lister_equipements()
    recherche_modif = request.query_params.get("modifier")
    equipement_a_modifier = db.trouver_equipement(recherche_modif) if recherche_modif else None

    erreur = request.query_params.get("erreur")
    if recherche_modif and equipement_a_modifier is None and not erreur:
        erreur = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche_modif} »"

    return templates.TemplateResponse(request, "inventaire.html", {
        "utilisateur": admin,
        "equipements": equipements,
        "groupes": db.lister_groupes(),
        "equipement_a_modifier": equipement_a_modifier,
        "erreur": erreur,
        "page_active": "inventaire",
    })


@app.post("/equipements")
async def ajouter_equipement_html(
    admin: Admin,
    nom: str = Form(...),
    adresse: str = Form(...),
    communaute_snmp: str = Form("public"),
    description: str = Form(""),
    groupe_id: str = Form(""),
):
    """Ajoute un équipement depuis le formulaire de la page Inventaire."""
    try:
        infos = await lire_infos_systeme(
            adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
        )
    except SnmpError as exc:
        return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}", status_code=303)

    try:
        db.creer_equipement(
            nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, description_systeme=infos["sysDescr"],
            groupe_id=int(groupe_id) if groupe_id else None,
        )
    except sqlite3.IntegrityError:
        message = f"Un équipement existe déjà pour « {adresse} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)

    return RedirectResponse("/inventaire", status_code=303)


@app.post("/equipements/supprimer")
async def supprimer_equipement_html(admin: Admin, recherche: str = Form(...)):
    """Supprime l'équipement trouvé par nom ou adresse depuis le formulaire de recherche."""
    equipement = db.trouver_equipement(recherche)
    if equipement is None:
        message = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)
    db.supprimer_equipement(equipement["id"])
    return RedirectResponse("/inventaire", status_code=303)


@app.post("/equipements/modifier")
async def modifier_equipement_html(
    admin: Admin,
    id_equipement: int = Form(...),
    nom: str = Form(...),
    adresse: str = Form(...),
    communaute_snmp: str = Form(...),
    description: str = Form(""),
    groupe_id: str = Form(""),
):
    """Met à jour un équipement existant depuis le formulaire de la page Inventaire."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    description_systeme = equipement["description_systeme"]
    if adresse != equipement["adresse"] or communaute_snmp != equipement["communaute_snmp"]:
        try:
            infos = await lire_infos_systeme(
                adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
            )
        except SnmpError as exc:
            return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}", status_code=303)
        description_systeme = infos["sysDescr"]

    try:
        db.modifier_equipement(
            id_equipement, nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, groupe_id=int(groupe_id) if groupe_id else None,
            description_systeme=description_systeme,
        )
    except sqlite3.IntegrityError:
        message = f"Un équipement existe déjà pour « {adresse} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)

    return RedirectResponse("/inventaire", status_code=303)


@app.post("/groupes")
async def creer_groupe_html(admin: Admin, nom: str = Form(...)):
    """Crée un groupe depuis le formulaire de la page Inventaire."""
    try:
        db.creer_groupe(nom)
    except sqlite3.IntegrityError:
        message = f"Le groupe « {nom} » existe déjà"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)
    return RedirectResponse("/inventaire", status_code=303)


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
            groupe_id=payload.groupe_id,
        )
    except sqlite3.IntegrityError:
        return JSONResponse(
            status_code=409, content={"erreur": f"Un équipement existe déjà pour « {payload.adresse} »"},
        )
    return {"id": id_equipement}
