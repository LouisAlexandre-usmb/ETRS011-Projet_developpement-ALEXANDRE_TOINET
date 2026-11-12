"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /
- API JSON             : /api/...
"""
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

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
    db.purger_mesures_anciennes()  # rattrape tout retard pris pendant un arrêt prolongé
    planificateur = AsyncIOScheduler()
    planificateur.add_job(collecte.verifier_echeances, "interval", seconds=TICK_PLANIFICATEUR)
    planificateur.add_job(db.purger_mesures_anciennes, "interval", hours=24)
    planificateur.start()
    yield
    planificateur.shutdown()


app = FastAPI(title="Supervision SNMP", version="0.6.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


FUSEAU_AFFICHAGE = ZoneInfo("Europe/Paris")


def formater_date_fr(valeur: str | None) -> str:
    """Convertit une date ISO stockée en base ("2026-11-05T08:16:27", en UTC puisque
    c'est ce que renvoie strftime('now') de SQLite) en heure locale lisible
    ("05/11/2026 09:16:27"), en gérant l'heure d'été/hiver."""
    if not valeur:
        return "—"
    try:
        instant_utc = datetime.fromisoformat(valeur).replace(tzinfo=timezone.utc)
        return instant_utc.astimezone(FUSEAU_AFFICHAGE).strftime("%d/%m/%Y %H:%M:%S")
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
        "groupes_vides": db.lister_groupes_vides(),
        "page_active": "tableau_de_bord",
    })


@app.get("/equipements/{id_equipement}", response_class=HTMLResponse)
async def fiche_equipement(request: Request, id_equipement: int):
    equipement = db.obtenir_equipement_detail(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    codes_actifs = {e["code"] for e in db.lister_elements_actifs(equipement["modele_supervision_id"])}

    return templates.TemplateResponse(request, "fiche_equipement.html", {
        "equipement": equipement,
        "parametres": db.obtenir_parametres_generaux(),
        "modeles": db.lister_modeles_supervision(),
        "codes_actifs": codes_actifs,
        "alertes_actives": db.lister_alertes("ACTIVE", id_equipement),
        "enfants": db.lister_enfants(id_equipement),
        "interfaces": db.lister_interfaces(id_equipement),
        "utilisations_interfaces": db.dernieres_valeurs_par_interface(id_equipement, "utilisation_interface"),
        "volumes": db.lister_volumes(id_equipement),
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
        return RedirectResponse(f"/equipements/{id_equipement}?ouvrir=collecte", status_code=303)

    if not intervalle_polling.isdigit() or int(intervalle_polling) <= 0:
        message = "L'intervalle de polling doit être un nombre de secondes positif"
        return RedirectResponse(
            f"/equipements/{id_equipement}?erreur={quote(message)}&ouvrir=collecte", status_code=303)

    db.modifier_intervalle_polling(id_equipement, int(intervalle_polling))
    return RedirectResponse(f"/equipements/{id_equipement}?ouvrir=collecte", status_code=303)


@app.post("/equipements/{id_equipement}/duree-conservation")
async def modifier_duree_conservation_html(id_equipement: int, duree_conservation: str = Form("")):
    """Change la durée de conservation des mesures d'un équipement (vide = valeur par défaut,
    30 jours maximum)."""
    if db.obtenir_equipement(id_equipement) is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    duree_conservation = duree_conservation.strip()
    if not duree_conservation:
        db.modifier_duree_conservation(id_equipement, None)
        return RedirectResponse(f"/equipements/{id_equipement}?ouvrir=collecte", status_code=303)

    if not duree_conservation.isdigit() or not (1 <= int(duree_conservation) <= 30):
        message = "La durée de conservation doit être comprise entre 1 et 30 jours"
        return RedirectResponse(
            f"/equipements/{id_equipement}?erreur={quote(message)}&ouvrir=collecte", status_code=303)

    db.modifier_duree_conservation(id_equipement, int(duree_conservation))
    return RedirectResponse(f"/equipements/{id_equipement}?ouvrir=collecte", status_code=303)


@app.post("/equipements/{id_equipement}/modele-supervision")
async def changer_modele_supervision_html(id_equipement: int, modele_supervision_id: int = Form(...)):
    """Change le modèle de supervision d'un équipement depuis sa fiche."""
    if db.obtenir_equipement(id_equipement) is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    db.changer_modele_supervision(id_equipement, modele_supervision_id)
    return RedirectResponse(f"/equipements/{id_equipement}?ouvrir=collecte", status_code=303)


@app.post("/interfaces/{id_interface}/basculer-visibilite")
async def basculer_visibilite_interface_html(id_interface: int):
    """Masque/réaffiche une interface sur la fiche de son équipement (n'affecte pas la collecte)."""
    id_equipement = db.basculer_visibilite_interface(id_interface)
    if id_equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Interface introuvable')}", status_code=303)
    return RedirectResponse(f"/equipements/{id_equipement}#interfaces", status_code=303)


@app.post("/volumes/{id_volume}/basculer-visibilite")
async def basculer_visibilite_volume_html(id_volume: int):
    """Masque/réaffiche un volume sur la fiche de son équipement (n'affecte pas la collecte)."""
    id_equipement = db.basculer_visibilite_volume(id_volume)
    if id_equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Volume introuvable')}", status_code=303)
    return RedirectResponse(f"/equipements/{id_equipement}#volumes", status_code=303)


@app.get("/alertes", response_class=HTMLResponse)
async def alertes(request: Request):
    """Configuration des seuils globaux et liste des alertes actives."""
    return templates.TemplateResponse(request, "alertes.html", {
        "seuils": db.lister_seuils_configurables(),
        "alertes": db.lister_alertes("ACTIVE"),
        "erreur": request.query_params.get("erreur"),
        "page_active": "alertes",
    })


@app.post("/seuils")
async def definir_seuil_html(metrique_id: int = Form(...), operateur: str = Form(...),
                              valeur_seuil: str = Form("")):
    """Crée, met à jour ou retire le seuil global d'une métrique (vide = aucun seuil)."""
    valeur_seuil = valeur_seuil.strip().replace(",", ".")
    if not valeur_seuil:
        db.supprimer_seuil(metrique_id)
        return RedirectResponse("/alertes", status_code=303)

    try:
        valeur = float(valeur_seuil)
    except ValueError:
        message = "La valeur du seuil doit être un nombre"
        return RedirectResponse(f"/alertes?erreur={quote(message)}", status_code=303)

    db.definir_seuil(metrique_id, operateur, valeur)
    return RedirectResponse("/alertes", status_code=303)


TAILLE_PAGE_LOGS = 50


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    """Historique unifié (changements de statut + erreurs de collecte), filtrable par
    équipement et par créneau de dates, paginé (50 lignes par page)."""
    equipement_id_filtre = request.query_params.get("equipement_id")
    equipement_id_filtre = int(equipement_id_filtre) if equipement_id_filtre else None

    depuis = request.query_params.get("depuis") or None
    jusqu_a = request.query_params.get("jusqu_a") or None
    jusqu_a_requete = f"{jusqu_a}T23:59:59" if jusqu_a else None  # inclut toute la journée

    page_brute = request.query_params.get("page", "1")
    page = int(page_brute) if page_brute.isdigit() else 1

    total = db.compter_logs(equipement_id_filtre, depuis, jusqu_a_requete)
    nb_pages = max(1, -(-total // TAILLE_PAGE_LOGS))  # division entière arrondie au-dessus
    page = max(1, min(page, nb_pages))

    filtre_qs = urlencode({k: v for k, v in {
        "equipement_id": equipement_id_filtre, "depuis": depuis, "jusqu_a": jusqu_a,
    }.items() if v})

    return templates.TemplateResponse(request, "logs.html", {
        "logs": db.lister_logs(equipement_id_filtre, depuis, jusqu_a_requete,
                                limite=TAILLE_PAGE_LOGS, decalage=(page - 1) * TAILLE_PAGE_LOGS),
        "equipements": db.lister_equipements(),
        "equipement_id_filtre": equipement_id_filtre,
        "depuis_filtre": depuis,
        "jusqu_a_filtre": jusqu_a,
        "page": page,
        "nb_pages": nb_pages,
        "total_logs": total,
        "filtre_qs": filtre_qs,
        "page_active": "logs",
    })


@app.get("/inventaire", response_class=HTMLResponse)
async def inventaire(request: Request):
    equipements = db.lister_equipements()
    recherche_modif = request.query_params.get("modifier")
    equipement_a_modifier = db.trouver_equipement(recherche_modif) if recherche_modif else None

    erreur = request.query_params.get("erreur")
    if recherche_modif and equipement_a_modifier is None and not erreur:
        erreur = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche_modif} »"

    # Les sections sont repliées par défaut ; on ne force l'ouverture de celle concernée
    # qu'en cas de recherche ou d'erreur (la query string "section" vient des redirections
    # ci-dessous, le fragment #ancre dans l'URL n'étant lui jamais envoyé au serveur).
    section_ouverte = request.query_params.get("section")
    if not section_ouverte and recherche_modif:
        section_ouverte = "modifier"

    return templates.TemplateResponse(request, "inventaire.html", {
        "equipements": equipements,
        "groupes": db.lister_groupes(),
        "modeles": db.lister_modeles_supervision(),
        "equipement_a_modifier": equipement_a_modifier,
        "erreur": erreur,
        "section_ouverte": section_ouverte,
        "page_active": "inventaire",
    })


@app.post("/equipements")
async def ajouter_equipement_html(
    nom: str = Form(...),
    adresse: str = Form(...),
    communaute_snmp: str = Form("public"),
    description: str = Form(""),
    groupe_id: str = Form(""),
    modele_supervision_id: int = Form(1),
):
    """Ajoute un équipement depuis le formulaire de la page Inventaire."""
    try:
        infos = await lire_infos_systeme(
            adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
        )
    except SnmpError as exc:
        return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}&section=ajouter#ajouter", status_code=303)

    try:
        db.creer_equipement(
            nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, description_systeme=infos["sysDescr"],
            groupe_id=int(groupe_id) if groupe_id else None,
            modele_supervision_id=modele_supervision_id,
        )
    except sqlite3.IntegrityError:
        message = f"Un équipement existe déjà pour « {adresse} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}&section=ajouter#ajouter", status_code=303)

    return RedirectResponse("/inventaire?section=ajouter#ajouter", status_code=303)


@app.post("/equipements/supprimer")
async def supprimer_equipement_html(recherche: str = Form(...)):
    """Supprime l'équipement trouvé par nom ou adresse depuis le formulaire de recherche."""
    equipement = db.trouver_equipement(recherche)
    if equipement is None:
        message = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}&section=supprimer#supprimer", status_code=303)
    db.supprimer_equipement(equipement["id"])
    return RedirectResponse("/inventaire?section=supprimer#supprimer", status_code=303)


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
        return RedirectResponse(
            f"/inventaire?erreur={quote('Équipement introuvable')}&section=modifier#modifier", status_code=303)

    description_systeme = equipement["description_systeme"]
    if adresse != equipement["adresse"] or communaute_snmp != equipement["communaute_snmp"]:
        try:
            infos = await lire_infos_systeme(
                adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
            )
        except SnmpError as exc:
            return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}&section=modifier#modifier", status_code=303)
        description_systeme = infos["sysDescr"]

    try:
        db.modifier_equipement(
            id_equipement, nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, groupe_id=int(groupe_id) if groupe_id else None,
            description_systeme=description_systeme,
        )
    except sqlite3.IntegrityError:
        message = f"Un équipement existe déjà pour « {adresse} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}&section=modifier#modifier", status_code=303)

    return RedirectResponse("/inventaire?section=modifier#modifier", status_code=303)


@app.post("/groupes")
async def creer_groupe_html(nom: str = Form(...)):
    """Crée un groupe depuis le formulaire de la page Inventaire."""
    try:
        db.creer_groupe(nom)
    except sqlite3.IntegrityError:
        message = f"Le groupe « {nom} » existe déjà"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}&section=groupes#groupes", status_code=303)
    return RedirectResponse("/inventaire?section=groupes#groupes", status_code=303)


@app.post("/groupes/{id_groupe}/supprimer")
async def supprimer_groupe_html(id_groupe: int):
    """Supprime un groupe vide depuis le tableau de bord (le bouton n'est affiché que
    pour les groupes sans équipement ; db.supprimer_groupe_si_vide revérifie quand même)."""
    db.supprimer_groupe_si_vide(id_groupe)
    return RedirectResponse("/", status_code=303)


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
    interface). Query params : metrique (code, ex. "cpu"), interface_id, depuis, jusqu_a.
    Un compteur de trafic par interface (if_in_octets/if_out_octets) est renvoyé comme un
    débit (octets/s), pas comme le compteur brut : cf. db.lister_debit."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})

    metrique_ligne = db.obtenir_metrique(metrique)
    if metrique_ligne and metrique_ligne["type_valeur"] == "COMPTEUR" and metrique_ligne["par_interface"]:
        return db.lister_debit(id_equipement, metrique, interface_id, depuis, jusqu_a)

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
