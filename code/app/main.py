"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : tableau de bord, fiche équipement, alertes, inventaire, connexion
- API JSON             : /api/...
"""
import itertools
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app import auth, collecte, config, db, metriques
from app.auth import AccesRefuse, NonConnecte, exiger_admin, utilisateur_courant
from app.snmp_client import SnmpError, lire_infos_systeme
from app.utilisateurs import Utilisateur

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # sinon une ligne toutes les 5 s


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.purger_donnees_anciennes()  # rattrape un retard pris pendant un arrêt prolongé
    collecte.demarrer()
    yield
    collecte.arreter()


app = FastAPI(title="Supervision SNMP", version="0.2.0", lifespan=lifespan)
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


def formater_date_fr(valeur: str | None) -> str:
    """Convertit une date ISO stockée en base, en UTC ("2026-11-05T08:16:27"), en heure locale
    lisible ("05/11/2026 09:16:27")."""
    if not valeur:
        return "—"
    try:
        date_utc = datetime.fromisoformat(valeur).replace(tzinfo=timezone.utc)
        return date_utc.astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return valeur


def version_statique() -> int:
    """Date du dernier fichier statique modifié, ajoutée à leurs liens : sans elle, le navigateur
    peut continuer d'utiliser une ancienne feuille de style gardée en cache."""
    return max(fichier.stat().st_mtime_ns for fichier in (BASE_DIR / "static").iterdir())


for environnement in (templates.env, auth.templates.env):  # base.html est aussi rendu par auth.py
    environnement.globals["version_statique"] = version_statique
templates.env.filters["date_fr"] = formater_date_fr
templates.env.globals["periode_rafraichissement"] = lambda: db.obtenir_parametres_generaux()["periode_rafraichissement"]


class NouvelEquipement(BaseModel):
    """Corps attendu par POST /api/equipements."""
    nom: str
    adresse: str
    communaute_snmp: str = "public"
    description: str | None = None
    groupe_id: int | None = None
    version_snmp: Literal["V1", "V2C"] = "V2C"
    parent_id: int | None = None
    modele_supervision_id: int | None = None   # absent = détection automatique


class SeuilEquipement(BaseModel):
    """Corps attendu par POST /api/equipements/{id}/seuils."""
    metrique_id: int
    valeur: float = Field(ge=0, le=100)
    valeur_critique: float = Field(ge=0, le=100)
    actif: bool = True


class SeuilAModifier(BaseModel):
    """Corps attendu par POST /api/seuils."""
    metrique_id: int
    valeur: float = Field(ge=0, le=100)
    valeur_critique: float = Field(ge=0, le=100)


# Réglage -> (libellé, colonne de la valeur globale, minimum, maximum). Le minimum de l'intervalle
# correspond à la fréquence de vérification du planificateur (collecte.PERIODE_VERIFICATION).
REGLAGES = {
    "intervalle_polling": ("Intervalle de polling (secondes)", "intervalle_polling_defaut", 5, 86400),
    "nb_echecs_alerte": ("Échecs consécutifs avant alerte", "nb_echecs_avant_indisponibilite", 1, 100),
    "duree_conservation": ("Conservation des données (jours)", "duree_conservation_detail", 1, 3650),
}


def _entier_borne(texte: str, champ: str) -> int | None:
    """Convertit une saisie de réglage ; None si elle n'est pas un entier dans les bornes."""
    minimum, maximum = REGLAGES[champ][2:]
    texte = texte.strip()
    return int(texte) if texte.isascii() and texte.isdigit() and minimum <= int(texte) <= maximum else None


def _message_bornes(champ: str) -> str:
    libelle, _, minimum, maximum = REGLAGES[champ]
    return f"{libelle} : un nombre entier entre {minimum} et {maximum}"


def _sections_tableau_de_bord(equipements: list, alertes: list, filtre_actif: bool) -> list[dict]:
    """Prépare les blocs du tableau de bord : « Tous », puis un par groupe, chacun avec le nombre
    d'équipements disponibles (bleu), en avertissement (orange) et critiques (rouge)."""
    niveaux: dict[int, str] = {}
    for alerte in alertes:
        if alerte["criticite"] == "CRITIQUE" or alerte["equipement_id"] not in niveaux:
            niveaux[alerte["equipement_id"]] = alerte["criticite"]

    def couleur(equipement) -> str | None:
        niveau = niveaux.get(equipement["id"])
        if niveau:
            return "rouge" if niveau == "CRITIQUE" else "orange"
        return {"OPERATIONNEL": "bleu", "DEGRADE": "orange", "INDISPONIBLE": "rouge"}.get(equipement["statut"])

    def section(identifiant: str, nom: str, liste: list) -> dict:
        couleurs = [couleur(equipement) for equipement in liste]
        return {
            "id": f"groupe-{identifiant}", "nom": nom, "equipements": liste,
            "ouvert": identifiant == "tous" or filtre_actif,
            **{teinte: couleurs.count(teinte) for teinte in ("bleu", "orange", "rouge")},
        }

    sections = [section("tous", "Tous", equipements)]
    # lister_equipements trie déjà par groupe, les équipements sans groupe en dernier
    for (groupe_id, groupe_nom), liste in itertools.groupby(equipements, key=lambda e: (e["groupe_id"], e["groupe_nom"])):
        sections.append(section(str(groupe_id or "aucun"), "Non groupés" if groupe_nom == "~" else groupe_nom, list(liste)))
    return sections


def _erreur_seuil_equipement(id_equipement: int, metrique_id: int, valeur: float,
                              valeur_critique: float) -> str | None:
    """Vérifie un seuil propre à un équipement ; renvoie le message d'erreur, ou None."""
    if metrique_id not in {seuil["metrique_id"] for seuil in db.seuils_de_l_equipement(id_equipement)}:
        return "Ce service n'est pas surveillé par cet équipement"
    if not 0 <= valeur <= valeur_critique <= 100:
        return "Seuils invalides : entre 0 et 100 %, l'avertissement ne dépassant pas le niveau critique"
    return None


def _erreur_configuration(id_equipement: int | None, version_snmp: str, parent_id: int | None,
                           modele_supervision_id: int | None = None) -> str | None:
    """Vérifie les choix d'un formulaire d'équipement ; renvoie le message d'erreur, ou None."""
    if version_snmp not in ("V1", "V2C"):
        return "Version SNMP inconnue"
    if parent_id is not None:
        if db.obtenir_equipement(parent_id) is None:
            return "Équipement parent introuvable"
        if id_equipement is not None and db.parent_creerait_un_cycle(id_equipement, parent_id):
            return "Relation impossible : un équipement ne peut pas dépendre de lui-même ou de ses enfants"
    if modele_supervision_id is not None and modele_supervision_id not in {m["id"] for m in db.lister_modeles()}:
        return "Modèle de supervision inconnu"
    return None


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
    recherche = request.query_params.get("recherche", "").strip()
    statut = request.query_params.get("statut", "")
    groupe = request.query_params.get("groupe", "")
    equipements_filtres = [
        e for e in equipements
        if (not recherche or recherche.lower() in e["nom"].lower() or recherche.lower() in e["adresse"].lower())
        and (not statut or e["statut"] == statut)
        and (not groupe or str(e["groupe_id"]) == groupe)
    ]
    alertes = db.lister_alertes(statut="ACTIVE")
    filtre_actif = bool(recherche or statut or groupe)
    return templates.TemplateResponse(request, "index.html", {
        "utilisateur": utilisateur,
        "equipements": equipements,
        "equipements_filtres": equipements_filtres,
        "sections": _sections_tableau_de_bord(equipements_filtres, alertes, filtre_actif) if equipements_filtres else [],
        "filtre_actif": filtre_actif,
        "groupes": db.lister_groupes(),
        "recherche": recherche,
        "statut": statut,
        "groupe": groupe,
        "alertes": alertes,
        "masquer_correlees": True,
        "page_active": "tableau_de_bord",
    })


@app.get("/equipements/{id_equipement}", response_class=HTMLResponse)
async def fiche_equipement(request: Request, id_equipement: int, utilisateur: Connecte):
    equipement = db.obtenir_equipement_detail(id_equipement)
    if equipement is None:
        return RedirectResponse("/", status_code=303)  # l'inventaire est réservé aux admins

    interfaces = db.lister_interfaces(id_equipement)
    return templates.TemplateResponse(request, "fiche_equipement.html", {
        "utilisateur": utilisateur,
        "equipement": equipement,
        "parametres": db.obtenir_parametres_generaux(),
        "enfants": db.lister_enfants(id_equipement),
        "interfaces": [itf for itf in interfaces if itf["suivi"] == "OUI"],
        "nb_a_valider": sum(itf["suivi"] == "A_VALIDER" and itf["presente"] for itf in interfaces),
        "modeles": db.lister_modeles(),
        "seuils": db.seuils_de_l_equipement(id_equipement),
        "services": db.services_de_l_equipement(id_equipement),
        "codes_actifs": {e["code"] for e in db.lister_elements_actifs(equipement["modele_supervision_id"])},
        "reglages": REGLAGES,
        "alertes": db.lister_alertes(statut="ACTIVE", equipement_id=id_equipement),
        "masquer_correlees": False,
        "historique_alertes": db.lister_alertes(statut="CLOTUREE", equipement_id=id_equipement, limite=20),
        "changements_statut": db.lister_changements_statut(id_equipement),
        "erreurs_collecte": db.lister_erreurs_collecte(id_equipement),
        "erreur": request.query_params.get("erreur"),
        "page_active": "fiche_equipement",
    })


@app.get("/alertes", response_class=HTMLResponse)
async def page_alertes(request: Request, utilisateur: Connecte):
    return templates.TemplateResponse(request, "alertes.html", {
        "utilisateur": utilisateur,
        "alertes": db.lister_alertes(statut="ACTIVE"),
        "masquer_correlees": True,
        "seuils": db.lister_seuils(),
        "criticites": db.lister_criticites(),
        "parametres": db.obtenir_parametres_generaux(),
        "reglages": REGLAGES,
        "nb_reglages_propres": db.compter_reglages_propres(),
        "historique": db.lister_alertes(statut="CLOTUREE", limite=50),
        "erreur": request.query_params.get("erreur"),
        "page_active": "alertes",
    })


@app.post("/seuils")
async def modifier_seuil_html(admin: Admin, metrique_id: int = Form(...), valeur: float = Form(...),
                               valeur_critique: float = Form(...)):
    """Enregistre les seuils d'un service et les applique aussitôt aux dernières mesures."""
    if (not 0 <= valeur <= valeur_critique <= 100
            or not db.modifier_seuil(metrique_id, valeur, valeur_critique)):
        message = "Seuils invalides : entre 0 et 100 %, l'avertissement ne dépassant pas le niveau critique"
        return RedirectResponse(f"/alertes?erreur={quote(message)}", status_code=303)
    collecte.reevaluer_seuils_du_parc()
    return RedirectResponse("/alertes#seuils-globaux", status_code=303)


@app.post("/criticites")
async def modifier_criticite_html(admin: Admin, code: str = Form(...), criticite: str = Form(...)):
    """Change la criticité d'un type d'alerte qui ne dépend pas d'un seuil."""
    if criticite not in ("AVERTISSEMENT", "CRITIQUE") or not db.modifier_criticite(code, criticite):
        return RedirectResponse(f"/alertes?erreur={quote('Criticité invalide')}", status_code=303)
    return RedirectResponse("/alertes#seuils-globaux", status_code=303)


@app.post("/equipements/{id_equipement}/seuils")
async def enregistrer_seuil_equipement_html(
    id_equipement: int, admin: Admin, metrique_id: int = Form(...), valeur: float = Form(...),
    valeur_critique: float = Form(...), actif: str = Form(""),
):
    """Enregistre le seuil propre à cet équipement pour un service ; décoché, il revient au global."""
    if actif:
        erreur = _erreur_seuil_equipement(id_equipement, metrique_id, valeur, valeur_critique)
        if erreur:
            return RedirectResponse(f"/equipements/{id_equipement}?erreur={quote(erreur)}#seuils", status_code=303)
        db.enregistrer_seuil_equipement(id_equipement, metrique_id, valeur, valeur_critique)
    else:
        db.supprimer_seuil_equipement(id_equipement, metrique_id)
    collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#seuils", status_code=303)


@app.post("/equipements/{id_equipement}/seuils/{metrique_id}/reinitialiser")
async def reinitialiser_seuil_equipement_html(id_equipement: int, metrique_id: int, admin: Admin):
    """Fait revenir un service de cet équipement au réglage global."""
    db.supprimer_seuil_equipement(id_equipement, metrique_id)
    collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#seuils", status_code=303)


@app.post("/equipements/{id_equipement}/modele")
async def changer_modele_html(id_equipement: int, admin: Admin, modele_supervision_id: int = Form(...)):
    """Corrige le modèle de supervision détecté automatiquement."""
    erreur = _erreur_configuration(None, "V2C", None, modele_supervision_id)
    if erreur:
        return RedirectResponse(f"/equipements/{id_equipement}?erreur={quote(erreur)}#collecte", status_code=303)
    db.changer_modele_supervision(id_equipement, modele_supervision_id)
    collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#collecte", status_code=303)


@app.get("/equipements/{id_equipement}/interfaces", response_class=HTMLResponse)
async def page_interfaces(request: Request, id_equipement: int, admin: Admin):
    """Choix des interfaces à surveiller, proposé à l'ajout puis accessible depuis la fiche."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    interfaces = db.lister_interfaces(id_equipement)
    presentes = [itf for itf in interfaces if itf["presente"]]
    return templates.TemplateResponse(request, "interfaces.html", {
        "utilisateur": admin,
        "equipement": equipement,
        "presentes": presentes,
        "absentes": [itf for itf in interfaces if not itf["presente"]],
        "proposees": {itf["id"] for itf in presentes if collecte.interface_proposee(itf)},
        "nb_a_valider": sum(itf["suivi"] == "A_VALIDER" for itf in presentes),
        "modele_non_identifie": "modele_non_identifie" in request.query_params,
        "erreur": request.query_params.get("erreur"),
    })


@app.post("/equipements/{id_equipement}/interfaces")
async def enregistrer_interfaces_html(
    id_equipement: int,
    admin: Admin,
    affichees: list[int] = Form(default=[]),
    surveillees: list[int] = Form(default=[]),
):
    """Enregistre les interfaces cochées ; les autres interfaces affichées sont ignorées."""
    db.enregistrer_choix_interfaces(id_equipement, affichees, surveillees)
    return RedirectResponse(f"/equipements/{id_equipement}", status_code=303)


@app.post("/equipements/{id_equipement}/interfaces/decouvrir")
async def decouvrir_interfaces_html(id_equipement: int, admin: Admin):
    """Relance immédiatement la recherche d'interfaces, sans attendre la prochaine collecte."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    cible = f"/equipements/{id_equipement}/interfaces"
    if not await collecte.decouvrir_interfaces(equipement, config.SNMP_TIMEOUT, config.SNMP_RETRIES):
        return RedirectResponse(f"{cible}?erreur={quote('Recherche impossible : équipement injoignable')}", status_code=303)
    return RedirectResponse(cible, status_code=303)


@app.post("/equipements/{id_equipement}/interfaces/{interface_id}/supprimer")
async def supprimer_interface_html(id_equipement: int, interface_id: int, admin: Admin):
    """Supprime une interface que l'équipement ne déclare plus, avec son historique."""
    cible = f"/equipements/{id_equipement}/interfaces"
    if not db.supprimer_interface_absente(id_equipement, interface_id):
        message = "Seule une interface disparue peut être supprimée"
        return RedirectResponse(f"{cible}?erreur={quote(message)}", status_code=303)
    return RedirectResponse(cible, status_code=303)


@app.post("/equipements/{id_equipement}/reglages")
async def modifier_reglage_equipement_html(id_equipement: int, admin: Admin, champ: str = Form(...),
                                            valeur: str = Form(""), actif: str = Form("")):
    """Enregistre un réglage propre à l'équipement ; décoché, il revient à la valeur globale."""
    cible = f"/equipements/{id_equipement}"
    if db.obtenir_equipement(id_equipement) is None or champ not in REGLAGES:
        return RedirectResponse(f"/inventaire?erreur={quote('Réglage introuvable')}", status_code=303)
    nombre = _entier_borne(valeur, champ) if actif else None
    if actif and nombre is None:
        return RedirectResponse(f"{cible}?erreur={quote(_message_bornes(champ))}#reglages", status_code=303)
    db.modifier_reglage_equipement(id_equipement, champ, nombre)
    collecte.oublier_echeances(id_equipement)
    return RedirectResponse(f"{cible}#reglages", status_code=303)


@app.post("/equipements/{id_equipement}/services/{metrique_id}")
async def modifier_reglage_service_html(id_equipement: int, metrique_id: int, admin: Admin,
                                         intervalle_polling: str = Form(""), nb_echecs: str = Form(""),
                                         actif: str = Form("")):
    """Enregistre les réglages propres à un service ; décoché, il suit ceux de l'équipement."""
    cible = f"/equipements/{id_equipement}"
    if metrique_id not in {service["metrique_id"] for service in db.services_de_l_equipement(id_equipement)}:
        message = "Ce service n'est pas surveillé par cet équipement"
        return RedirectResponse(f"{cible}?erreur={quote(message)}#reglages", status_code=303)
    intervalle, echecs = None, None
    if actif:
        if intervalle_polling.strip():
            intervalle = _entier_borne(intervalle_polling, "intervalle_polling")
            if intervalle is None:
                return RedirectResponse(f"{cible}?erreur={quote(_message_bornes('intervalle_polling'))}#reglages", status_code=303)
        if nb_echecs.strip():
            echecs = _entier_borne(nb_echecs, "nb_echecs_alerte")
            if echecs is None:
                return RedirectResponse(f"{cible}?erreur={quote(_message_bornes('nb_echecs_alerte'))}#reglages", status_code=303)
    if intervalle is None and echecs is None:
        db.supprimer_reglage_service(id_equipement, metrique_id)
    else:
        db.enregistrer_reglage_service(id_equipement, metrique_id, intervalle, echecs)
    collecte.oublier_echeances(id_equipement)
    return RedirectResponse(f"{cible}#reglages", status_code=303)


@app.post("/parametres")
async def modifier_parametres_html(admin: Admin, intervalle_polling: str = Form(""),
                                    nb_echecs_alerte: str = Form(""), duree_conservation: str = Form("")):
    """Enregistre les réglages globaux de collecte et de conservation."""
    saisies = {"intervalle_polling": intervalle_polling, "nb_echecs_alerte": nb_echecs_alerte,
               "duree_conservation": duree_conservation}
    valeurs = {champ: _entier_borne(texte, champ) for champ, texte in saisies.items()}
    for champ, valeur in valeurs.items():
        if valeur is None:
            return RedirectResponse(f"/alertes?erreur={quote(_message_bornes(champ))}#parametres", status_code=303)
    db.modifier_parametres(valeurs["intervalle_polling"], valeurs["nb_echecs_alerte"], valeurs["duree_conservation"])
    collecte.oublier_echeances()
    return RedirectResponse("/alertes#parametres", status_code=303)


@app.get("/historique", response_class=HTMLResponse)
async def page_historique(request: Request, utilisateur: Connecte):
    """Changements de statut et alertes, avec recherche et filtre par catégorie."""
    recherche = request.query_params.get("recherche", "").strip()
    categorie = request.query_params.get("categorie", "")
    limite = 300
    return templates.TemplateResponse(request, "historique.html", {
        "utilisateur": utilisateur,
        "evenements": db.lister_historique(
            categorie if categorie in ("STATUT", "AVERTISSEMENT", "CRITIQUE") else None, recherche, limite,
        ),
        "recherche": recherche,
        "categorie": categorie,
        "limite": limite,
        "page_active": "historique",
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
        "modeles": db.lister_modeles(),
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
    version_snmp: str = Form("V2C"),
    parent_id: str = Form(""),
    modele_supervision_id: str = Form(""),
):
    """Ajoute un équipement depuis le formulaire de la page Inventaire."""
    id_parent = int(parent_id) if parent_id else None
    id_modele = int(modele_supervision_id) if modele_supervision_id else None
    erreur = _erreur_configuration(None, version_snmp, id_parent, id_modele)
    if erreur:
        return RedirectResponse(f"/inventaire?erreur={quote(erreur)}", status_code=303)

    try:
        infos = await lire_infos_systeme(
            adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
            version=version_snmp,
        )
    except SnmpError as exc:
        return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}", status_code=303)

    sys_object_id = infos.get("sysObjectID")
    modele_non_identifie = False
    if id_modele is None:
        id_modele, identifie = await collecte.detecter_modele(adresse, communaute_snmp, version_snmp, sys_object_id)
        modele_non_identifie = not identifie

    try:
        id_equipement = db.creer_equipement(
            nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, description_systeme=infos["sysDescr"],
            groupe_id=int(groupe_id) if groupe_id else None,
            version_snmp=version_snmp, parent_id=id_parent, sys_object_id=sys_object_id,
            modele_supervision_id=id_modele,
        )
    except sqlite3.IntegrityError:
        message = f"Un équipement existe déjà pour « {adresse} »"
        return RedirectResponse(f"/inventaire?erreur={quote(message)}", status_code=303)

    # On enchaîne sur le choix des interfaces ; en cas d'échec, la collecte les découvrira plus tard
    await collecte.decouvrir_interfaces(
        db.obtenir_equipement(id_equipement), config.SNMP_TIMEOUT, config.SNMP_RETRIES,
    )
    suite = "?modele_non_identifie=1" if modele_non_identifie else ""
    return RedirectResponse(f"/equipements/{id_equipement}/interfaces{suite}", status_code=303)


@app.post("/equipements/supprimer")
async def supprimer_equipement_html(admin: Admin, recherche: str = Form(...)):
    """Supprime l'équipement désigné par son nom ou son adresse exacts."""
    equipement = db.trouver_equipement(recherche, exact=True)
    if equipement is None:
        message = f"Aucun équipement (ou plusieurs) ne porte exactement le nom ou l'adresse « {recherche} »"
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
    version_snmp: str = Form("V2C"),
    parent_id: str = Form(""),
):
    """Met à jour un équipement existant depuis le formulaire de la page Inventaire."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)

    id_parent = int(parent_id) if parent_id else None
    erreur = _erreur_configuration(id_equipement, version_snmp, id_parent)
    if erreur:
        return RedirectResponse(f"/inventaire?erreur={quote(erreur)}", status_code=303)

    description_systeme = equipement["description_systeme"]
    sys_object_id = equipement["sys_object_id"]
    if (adresse != equipement["adresse"] or communaute_snmp != equipement["communaute_snmp"]
            or version_snmp != equipement["version_snmp"]):
        try:
            infos = await lire_infos_systeme(
                adresse, communaute_snmp, timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
                version=version_snmp,
            )
        except SnmpError as exc:
            return RedirectResponse(f"/inventaire?erreur={quote(str(exc))}", status_code=303)
        description_systeme = infos["sysDescr"]
        sys_object_id = infos.get("sysObjectID")

    try:
        db.modifier_equipement(
            id_equipement, nom=nom, adresse=adresse, communaute_snmp=communaute_snmp,
            description=description or None, groupe_id=int(groupe_id) if groupe_id else None,
            description_systeme=description_systeme,
            version_snmp=version_snmp, parent_id=id_parent, sys_object_id=sys_object_id,
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


@app.post("/groupes/{id_groupe}/supprimer")
async def supprimer_groupe_html(id_groupe: int, admin: Admin):
    """Supprime un groupe depuis la page Inventaire ; ses équipements restent, sans groupe."""
    if not db.supprimer_groupe(id_groupe):
        return RedirectResponse(f"/inventaire?erreur={quote('Groupe introuvable')}#groupes", status_code=303)
    return RedirectResponse("/inventaire#groupes", status_code=303)


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
    """Liste des équipements enregistrés (sans la communauté SNMP, qui sert de mot de passe)."""
    return [
        {cle: ligne[cle] for cle in ligne.keys() if cle != "communaute_snmp"}
        for ligne in db.lister_equipements()
    ]


@app.get("/api/equipements/{id_equipement}/mesures")
async def mesures_equipement_api(id_equipement: int, metrique: str, utilisateur: Connecte,
                                  interface_id: int | None = None,
                                  depuis: str | None = None, jusqu_a: str | None = None):
    """Historique des relevés d'une métrique pour un équipement (et, si fournie, une
    interface). Query params : metrique (code, ex. "cpu"), interface_id, depuis, jusqu_a."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    mesures = db.lister_mesures(id_equipement, metrique, interface_id, depuis, jusqu_a)
    return [dict(ligne) for ligne in mesures]


@app.get("/api/equipements/{id_equipement}/debit")
async def debit_equipement_api(id_equipement: int, metrique: Literal["if_in_octets", "if_out_octets"],
                                interface_id: int, utilisateur: Connecte,
                                depuis: str | None = None, jusqu_a: str | None = None):
    """Débit d'une interface en bits/s, calculé à partir des relevés bruts de ses compteurs."""
    interface = next((itf for itf in db.lister_interfaces(id_equipement) if itf["id"] == interface_id), None)
    if interface is None:
        return JSONResponse(status_code=404, content={"erreur": "Interface introuvable"})
    mesures = db.lister_mesures(id_equipement, metrique, interface_id, depuis, jusqu_a)
    return metriques.calculer_debit(mesures, interface["vitesse"])


@app.get("/api/alertes")
async def lister_alertes_api(utilisateur: Connecte, statut: Literal["ACTIVE", "CLOTUREE"] | None = None):
    """Liste des alertes, éventuellement filtrée par statut."""
    return [dict(ligne) for ligne in db.lister_alertes(statut=statut)]


@app.get("/api/seuils")
async def lister_seuils_api(utilisateur: Connecte):
    """Liste des seuils configurés."""
    return [dict(ligne) for ligne in db.lister_seuils()]


@app.get("/api/equipements/{id_equipement}/seuils")
async def seuils_equipement_api(id_equipement: int, utilisateur: Connecte):
    """Seuils qui s'appliquent à un équipement ; `personnalise` indique un seuil propre."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    return [dict(ligne) for ligne in db.seuils_de_l_equipement(id_equipement)]


@app.post("/api/equipements/{id_equipement}/seuils")
async def enregistrer_seuil_equipement_api(id_equipement: int, payload: SeuilEquipement, admin: Admin):
    """Enregistre le seuil propre à cet équipement pour un service ; avec actif=false, il revient au global."""
    if payload.actif:
        erreur = _erreur_seuil_equipement(id_equipement, payload.metrique_id, payload.valeur, payload.valeur_critique)
        if erreur:
            return JSONResponse(status_code=400, content={"erreur": erreur})
        db.enregistrer_seuil_equipement(id_equipement, payload.metrique_id, payload.valeur, payload.valeur_critique)
    else:
        db.supprimer_seuil_equipement(id_equipement, payload.metrique_id)
    collecte.reevaluer_seuils(id_equipement)
    return {"statut": "ok"}


@app.delete("/api/equipements/{id_equipement}/seuils/{metrique_id}")
async def supprimer_seuil_equipement_api(id_equipement: int, metrique_id: int, admin: Admin):
    """Fait revenir un service de cet équipement au réglage global."""
    db.supprimer_seuil_equipement(id_equipement, metrique_id)
    collecte.reevaluer_seuils(id_equipement)
    return {"statut": "ok"}


@app.post("/api/seuils")
async def modifier_seuil_api(payload: SeuilAModifier, admin: Admin):
    """Modifie les seuils d'un service."""
    if payload.valeur > payload.valeur_critique:
        return JSONResponse(status_code=400, content={"erreur": "L'avertissement dépasse le niveau critique"})
    if not db.modifier_seuil(payload.metrique_id, payload.valeur, payload.valeur_critique):
        return JSONResponse(status_code=404, content={"erreur": "Seuil introuvable"})
    collecte.reevaluer_seuils_du_parc()
    return {"statut": "ok"}


@app.post("/api/equipements", status_code=201)
async def creer_equipement_api(payload: NouvelEquipement, admin: Admin):
    """Ajoute un équipement, après un test de connectivité SNMP (lecture de sysDescr)."""
    erreur = _erreur_configuration(None, payload.version_snmp, payload.parent_id, payload.modele_supervision_id)
    if erreur:
        return JSONResponse(status_code=400, content={"erreur": erreur})

    try:
        infos = await lire_infos_systeme(
            payload.adresse, payload.communaute_snmp,
            timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES, version=payload.version_snmp,
        )
    except SnmpError as exc:
        return JSONResponse(status_code=400, content={"erreur": str(exc)})

    sys_object_id = infos.get("sysObjectID")
    id_modele, modele_non_identifie = payload.modele_supervision_id, False
    if id_modele is None:
        id_modele, identifie = await collecte.detecter_modele(
            payload.adresse, payload.communaute_snmp, payload.version_snmp, sys_object_id,
        )
        modele_non_identifie = not identifie

    try:
        id_equipement = db.creer_equipement(
            nom=payload.nom, adresse=payload.adresse, communaute_snmp=payload.communaute_snmp,
            description=payload.description, description_systeme=infos["sysDescr"],
            groupe_id=payload.groupe_id, version_snmp=payload.version_snmp, parent_id=payload.parent_id,
            sys_object_id=sys_object_id, modele_supervision_id=id_modele,
        )
    except sqlite3.IntegrityError:
        return JSONResponse(
            status_code=409, content={"erreur": f"Un équipement existe déjà pour « {payload.adresse} »"},
        )
    return {"id": id_equipement, "modele_non_identifie": modele_non_identifie}
