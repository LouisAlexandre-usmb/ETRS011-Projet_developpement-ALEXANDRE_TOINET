"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /
- API JSON             : /api/...
"""
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import collecte, config, db
from app.snmp_client import SnmpError, lire_infos_systeme

BASE_DIR = Path(__file__).resolve().parent

# Fréquence à laquelle le planificateur vérifie quels équipements sont dus pour une collecte.
# Volontairement plus courte que l'intervalle de polling (pour rester proche de l'intervalle
# demandé), mais ce n'est pas un réglage métier : ça ne figure pas dans parametres_generaux.
TICK_PLANIFICATEUR = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    planificateur = AsyncIOScheduler()
    planificateur.add_job(collecte.verifier_echeances, "interval", seconds=TICK_PLANIFICATEUR)
    planificateur.start()
    yield
    planificateur.shutdown()


app = FastAPI(title="Supervision SNMP", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def formater_date_fr(valeur: str | None) -> str:
    """Convertit une date ISO stockée en base ("2026-11-05T08:16:27") en format lisible
    ("05/11/2026 08:16:27")."""
    if not valeur:
        return "—"
    try:
        return datetime.fromisoformat(valeur).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return valeur


templates.env.filters["date_fr"] = formater_date_fr


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
async def tableau_de_bord(request: Request):
    equipements = db.lister_equipements()
    return templates.TemplateResponse(request, "index.html", {
        "equipements": equipements,
        "page_active": "tableau_de_bord",
    })


@app.get("/equipements/{id_equipement}", response_class=HTMLResponse)
async def fiche_equipement(request: Request, id_equipement: int):
    equipement = db.obtenir_equipement_detail(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    return templates.TemplateResponse(request, "fiche_equipement.html", {
        "equipement": equipement,
        "parametres": db.obtenir_parametres_generaux(),
        "enfants": db.lister_enfants(id_equipement),
        "interfaces": db.lister_interfaces(id_equipement),
        "changements_statut": db.lister_changements_statut(id_equipement),
        "erreurs_collecte": db.lister_erreurs_collecte(id_equipement),
        "erreur": request.query_params.get("erreur"),
        "page_active": "fiche_equipement",
    })


@app.post("/equipements/{id_equipement}/intervalle-polling")
async def modifier_intervalle_polling_html(id_equipement: int, intervalle_polling: str = Form("")):
    """Change l'intervalle de polling d'un équipement (champ vide = valeur globale)."""
    if db.obtenir_equipement(id_equipement) is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    intervalle_polling = intervalle_polling.strip()
    if not intervalle_polling:
        db.modifier_intervalle_polling(id_equipement, None)
        return RedirectResponse(f"/equipements/{id_equipement}#collecte", status_code=303)

    if not intervalle_polling.isdigit() or int(intervalle_polling) <= 0:
        message = "L'intervalle de polling doit être un nombre de secondes positif"
        return RedirectResponse(f"/equipements/{id_equipement}?erreur={quote(message)}#collecte", status_code=303)

    db.modifier_intervalle_polling(id_equipement, int(intervalle_polling))
    return RedirectResponse(f"/equipements/{id_equipement}#collecte", status_code=303)


@app.post("/interfaces/{id_interface}/basculer-visibilite")
async def basculer_visibilite_interface_html(id_interface: int):
    """Masque/réaffiche une interface sur la fiche de son équipement (n'affecte pas la collecte)."""
    id_equipement = db.basculer_visibilite_interface(id_interface)
    if id_equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Interface introuvable')}", status_code=303)
    return RedirectResponse(f"/equipements/{id_equipement}#interfaces", status_code=303)


@app.get("/inventaire", response_class=HTMLResponse)
async def inventaire(request: Request):
    equipements = db.lister_equipements()
    recherche_modif = request.query_params.get("modifier")
    equipement_a_modifier = db.trouver_equipement(recherche_modif) if recherche_modif else None

    erreur = request.query_params.get("erreur")
    if recherche_modif and equipement_a_modifier is None and not erreur:
        erreur = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche_modif} »"

    return templates.TemplateResponse(request, "inventaire.html", {
        "equipements": equipements,
        "groupes": db.lister_groupes(),
        "equipement_a_modifier": equipement_a_modifier,
        "erreur": erreur,
        "page_active": "inventaire",
    })


@app.post("/equipements")
async def ajouter_equipement_html(
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
async def supprimer_equipement_html(recherche: str = Form(...)):
    """Supprime l'équipement trouvé par nom ou adresse depuis le formulaire de recherche."""
    equipement = db.trouver_equipement(recherche)
    if equipement is None:
        message = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)
    db.supprimer_equipement(equipement["id"])
    return RedirectResponse("/inventaire", status_code=303)


@app.post("/equipements/modifier")
async def modifier_equipement_html(
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
async def creer_groupe_html(nom: str = Form(...)):
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


@app.get("/api/equipements/{id_equipement}/mesures")
async def mesures_equipement_api(id_equipement: int, metrique: str,
                                  interface_id: int | None = None,
                                  depuis: str | None = None, jusqu_a: str | None = None):
    """Historique des relevés d'une métrique pour un équipement (et, si fournie, une
    interface). Query params : metrique (code, ex. "cpu"), interface_id, depuis, jusqu_a."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    mesures = db.lister_mesures(id_equipement, metrique, interface_id, depuis, jusqu_a)
    return [dict(ligne) for ligne in mesures]


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
            groupe_id=payload.groupe_id,
        )
    except sqlite3.IntegrityError:
        return JSONResponse(
            status_code=409, content={"erreur": f"Un équipement existe déjà pour « {payload.adresse} »"},
        )
    return {"id": id_equipement}
