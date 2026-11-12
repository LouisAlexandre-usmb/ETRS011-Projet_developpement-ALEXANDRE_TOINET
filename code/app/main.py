"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : tableau de bord, fiche équipement, alertes, inventaire, connexion
- API JSON             : /api/...
"""
import itertools
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import auth, collecte, config, db, internet, metriques, seuils
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
    internet.demarrer()
    collecte.demarrer()
    yield
    collecte.arreter()
    internet.arreter()


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


def formater_taille(octets) -> str:
    """Taille lisible en unités binaires : 1536 -> « 1.5 KB », 3.2e12 -> « 2.91 TB »."""
    if octets is None:
        return "—"
    taille = float(octets)
    for unite in ("B", "KB", "MB", "GB", "TB"):
        if abs(taille) < 1024 or unite == "TB":
            return f"{taille:.0f} {unite}" if unite == "B" else f"{taille:.2f} {unite}".replace(".00 ", " ")
        taille /= 1024
    return f"{taille:.2f} TB"


templates.env.filters["taille"] = formater_taille
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
    """Corps attendu par POST /api/equipements/{id}/seuils. `cible` : « m<id> » pour un service de
    mesure, « o<id> » pour un objet détecté (voir GET /api/equipements/{id}/seuils)."""
    cible: str
    operateur: Literal[">", "<", "="] = ">"
    avertissement: float | None = None
    critique: float | None = None
    actif: bool = True


# Réglage -> (libellé, colonne de la valeur globale, minimum, maximum). Le minimum de l'intervalle
# correspond à la fréquence de vérification du planificateur (collecte.PERIODE_VERIFICATION).
REGLAGES = {
    "intervalle_polling": ("Intervalle de polling (secondes)", "intervalle_polling_defaut", 5, 86400),
    "nb_echecs_alerte": ("Échecs consécutifs avant alerte", "nb_echecs_avant_indisponibilite", 1, 100),
    "duree_conservation": ("Conservation des données détaillées (jours)", "duree_conservation_detail", 1, 3650),
    "duree_conservation_agregee": ("Conservation des données agrégées (jours)", "duree_conservation_agregee", 1, 3650),
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


MAX_LIGNES_TABLE = 30


def _objets_de_la_fiche(id_equipement: int) -> dict:
    """Prépare les listes et les graphiques de la fiche à partir des objets détectés (ceux que
    l'admin a choisi de garder)."""
    tous = db.lister_objets_detectes(id_equipement)
    objets = [objet for objet in tous if objet["garde"]]
    valeurs: dict[int, dict[str, str]] = {}
    for ligne in db.lister_valeurs_objets(id_equipement):
        valeurs.setdefault(ligne["objet_id"], {})[ligne["instance"]] = ligne["valeur"]

    def affichage(objet, valeur):
        return json.loads(objet["etats"] or "{}").get(str(valeur), valeur) if valeur is not None else "—"

    simples, tableaux, graphiques = [], {}, []
    for objet in objets:
        # Un capteur qui porte le nom de sa propre table est une valeur unique (température
        # du système, état de l'alimentation...) : il rejoint les valeurs simples
        if objet["table_nom"] and objet["table_nom"] != objet["objet"]:
            # Une même table regroupe ses colonnes, même relevées par des capteurs différents
            # (température et état d'une alimentation, par exemple)
            tableaux.setdefault((objet["modele_nom"], objet["table_nom"]), []).append(objet)
        else:
            simples.append({"mib": objet["mib"], "nom": objet["objet"], "description": objet["description"],
                            "valeur": affichage(objet, next(iter(valeurs.get(objet["id"], {}).values()), None))})

    listes = []
    for (_, table), colonnes in tableaux.items():
        mib = ", ".join(dict.fromkeys(c["mib"] for c in colonnes))
        visibles = [c for c in colonnes if not collecte.OBJET_INDEX.search(c["objet"])] or colonnes
        instances = sorted({i for c in colonnes for i in valeurs.get(c["id"], {})},
                           key=lambda i: [int(p) for p in i.split(".") if p.isdigit()])
        listes.append({
            "mib": mib, "nom": table, "colonnes": [c["objet"] for c in visibles], "total": len(instances),
            "descriptions": [c["description"] for c in visibles],
            "lignes": [[affichage(c, valeurs.get(c["id"], {}).get(i)) for c in visibles] for i in instances[:MAX_LIGNES_TABLE]],
        })
    avec_historique = db.objets_avec_historique(id_equipement)
    for objet in objets:
        if objet["id"] in avec_historique:
            graphiques.append({"id": objet["id"], "nom": objet["objet"], "mib": objet["mib"]})
    return {"simples": simples, "listes": listes, "graphiques": graphiques, "nb_ecartes": len(tous) - len(objets),
            "nb_total": len(tous)}


def _enregistrer_seuil(id_equipement: int, cible: str, operateur: str, avertissement: float | None,
                       critique: float | None, actif: bool) -> str | None:
    """Vérifie et enregistre le seuil d'un service gardé de cet équipement ; renvoie l'erreur, ou None."""
    trouvee = next((c for c in collecte.cibles_de_seuil(id_equipement) if c["cle"] == cible), None)
    if trouvee is None:
        return "Ce service n'est pas relevé sur cet équipement"
    erreur = seuils.valider(trouvee["profil"], operateur, avertissement, critique, actif)
    if erreur:
        return erreur
    db.enregistrer_seuil(id_equipement, trouvee["metrique_id"], trouvee["objet_id"], operateur,
                         avertissement, critique, actif)
    collecte.reevaluer_seuils(id_equipement)
    return None


def _nombre_saisi(valeur: str) -> float | None:
    """Champ de formulaire facultatif -> nombre (la virgule décimale est acceptée)."""
    valeur = (valeur or "").strip().replace(",", ".")
    return float(valeur) if valeur else None


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
        "sante": {statut: sum(e["statut"] == statut for e in equipements)
                  for statut in ("OPERATIONNEL", "DEGRADE", "INDISPONIBLE", "EN_ATTENTE")},
        "acces_internet": internet.acces_internet(),
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
        "cibles_seuil": (cibles := collecte.cibles_de_seuil(id_equipement)),
        "valeurs_actuelles": collecte.valeurs_actuelles(id_equipement, cibles),
        "operateurs": seuils.OPERATEURS,
        "codes_actifs": {m["code"] for m in db.metriques_du_modele(id_equipement) if m["garde"]},
        "objets": _objets_de_la_fiche(id_equipement),
        "dernieres": db.dernieres_mesures(id_equipement),
        "volumes": db.derniers_volumes(id_equipement),
        "identification": "identification" in request.query_params,
        "nb_modeles_objets": len(db.lister_modeles_objets()),
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
        "parametres": db.obtenir_parametres_generaux(),
        "reglages": REGLAGES,
        "nb_reglages_propres": db.compter_reglages_propres(),
        "historique": db.lister_alertes(statut="CLOTUREE", limite=50),
        "erreur": request.query_params.get("erreur"),
        "page_active": "alertes",
    })


@app.post("/equipements/{id_equipement}/seuils")
async def enregistrer_seuil_equipement_html(
    id_equipement: int, admin: Admin, cible: str = Form(...), operateur: str = Form(">"),
    avertissement: str = Form(""), critique: str = Form(""), actif: str = Form(""),
):
    """Enregistre le seuil d'un service de cet équipement (mesure ou objet détecté)."""
    try:
        erreur = _enregistrer_seuil(id_equipement, cible, operateur, _nombre_saisi(avertissement),
                                    _nombre_saisi(critique), bool(actif))
    except ValueError:
        erreur = "Valeur numérique invalide"
    if erreur:
        return RedirectResponse(f"/equipements/{id_equipement}?erreur={quote(erreur)}#seuils", status_code=303)
    return RedirectResponse(f"/equipements/{id_equipement}#seuils", status_code=303)


@app.post("/equipements/{id_equipement}/seuils/remise-a-zero")
async def remettre_a_zero_seuils_html(id_equipement: int, admin: Admin):
    """Retire tous les seuils réglés sur cet équipement : CPU, RAM et disque reviennent à leurs
    valeurs par défaut, les autres services n'ont plus de seuil."""
    db.supprimer_seuils_equipement(id_equipement)
    collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#seuils", status_code=303)


@app.post("/equipements/{id_equipement}/seuils/reinitialiser")
async def reinitialiser_seuil_equipement_html(id_equipement: int, admin: Admin, cible: str = Form(...)):
    """Retire le seuil réglé : CPU, RAM et disque reviennent à leur valeur par défaut."""
    trouvee = next((c for c in collecte.cibles_de_seuil(id_equipement) if c["cle"] == cible), None)
    if trouvee is not None:
        db.supprimer_seuil(id_equipement, trouvee["metrique_id"], trouvee["objet_id"])
        collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#seuils", status_code=303)


@app.post("/equipements/{id_equipement}/modele/identifier")
async def identifier_modele_html(id_equipement: int, admin: Admin):
    """Relance l'identification du modèle : constructeur, capteurs, CPU et RAM."""
    if db.obtenir_equipement(id_equipement) is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    collecte.lancer_identification([id_equipement])
    return RedirectResponse(f"/equipements/{id_equipement}?identification=1#collecte", status_code=303)


@app.post("/equipements/{id_equipement}/modele")
async def changer_modele_html(id_equipement: int, admin: Admin, modele_supervision_id: int = Form(...)):
    """Corrige le modèle de supervision détecté automatiquement."""
    erreur = _erreur_configuration(None, "V2C", None, modele_supervision_id)
    if erreur:
        return RedirectResponse(f"/equipements/{id_equipement}?erreur={quote(erreur)}#collecte", status_code=303)
    db.changer_modele_supervision(id_equipement, modele_supervision_id)
    collecte.reevaluer_seuils(id_equipement)
    collecte.lancer_detection(id_equipement)
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
        "identification": "identification" in request.query_params,
        "acces_internet": internet.acces_internet(),
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
    collecte.reevaluer_interfaces(id_equipement)   # une interface suivie en panne alerte tout de suite
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


@app.post("/parametres")
async def modifier_parametres_html(admin: Admin, intervalle_polling: str = Form(""),
                                    nb_echecs_alerte: str = Form(""), duree_conservation: str = Form(""),
                                    duree_conservation_agregee: str = Form("")):
    """Enregistre les réglages globaux de collecte et de conservation."""
    saisies = {"intervalle_polling": intervalle_polling, "nb_echecs_alerte": nb_echecs_alerte,
               "duree_conservation": duree_conservation, "duree_conservation_agregee": duree_conservation_agregee}
    valeurs = {champ: _entier_borne(texte, champ) for champ, texte in saisies.items()}
    for champ, valeur in valeurs.items():
        if valeur is None:
            return RedirectResponse(f"/alertes?erreur={quote(_message_bornes(champ))}#parametres", status_code=303)
    db.modifier_parametres(valeurs["intervalle_polling"], valeurs["nb_echecs_alerte"], valeurs["duree_conservation"],
                           valeurs["duree_conservation_agregee"])
    collecte.oublier_echeances()
    return RedirectResponse("/alertes#parametres", status_code=303)


TAILLE_PAGE_LOGS = 50


@app.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request, utilisateur: Connecte):
    """Fil unifié des logs (changements de statut, erreurs de collecte, alertes), filtrable par
    équipement, créneau de dates, catégorie et texte, et paginé."""
    parametres = request.query_params
    equipement_id = int(parametres["equipement_id"]) if parametres.get("equipement_id", "").isdigit() else None
    depuis = parametres.get("depuis") or None
    jusqu_a = parametres.get("jusqu_a") or None
    categorie = parametres.get("categorie", "") if parametres.get("categorie", "") in db.CATEGORIES_LOGS else ""
    recherche = parametres.get("recherche", "").strip()
    filtres = {"equipement_id": equipement_id, "depuis": depuis,
               "jusqu_a": f"{jusqu_a}T23:59:59" if jusqu_a else None,   # inclut toute la journée
               "categorie": categorie or None, "recherche": recherche or None}
    total = db.compter_logs(**filtres)
    nb_pages = max(1, -(-total // TAILLE_PAGE_LOGS))   # division arrondie au-dessus
    page = max(1, min(int(parametres["page"]) if parametres.get("page", "").isdigit() else 1, nb_pages))
    filtre_qs = urlencode({cle: valeur for cle, valeur in {
        "equipement_id": equipement_id, "depuis": depuis, "jusqu_a": jusqu_a, "categorie": categorie,
        "recherche": recherche}.items() if valeur})
    return templates.TemplateResponse(request, "logs.html", {
        "utilisateur": utilisateur,
        "logs": db.lister_logs(**filtres, limite=TAILLE_PAGE_LOGS, decalage=(page - 1) * TAILLE_PAGE_LOGS),
        "equipements": db.lister_equipements(),
        "equipement_id_filtre": equipement_id, "depuis_filtre": depuis, "jusqu_a_filtre": jusqu_a,
        "categorie": categorie, "recherche": recherche,
        "page": page, "nb_pages": nb_pages, "total_logs": total, "filtre_qs": filtre_qs,
        "page_active": "logs",
    })


@app.get("/historique")
async def ancien_historique(request: Request, utilisateur: Connecte):
    """L'onglet Historique est devenu Logs."""
    return RedirectResponse(f"/logs?{request.url.query}" if request.url.query else "/logs", status_code=303)


@app.get("/inventaire", response_class=HTMLResponse)
async def inventaire(request: Request, admin: Admin):
    equipements = db.lister_equipements()
    recherche_modif = request.query_params.get("modifier")
    equipement_a_modifier = db.trouver_equipement(recherche_modif) if recherche_modif else None

    erreur = request.query_params.get("erreur")
    if recherche_modif and equipement_a_modifier is None and not erreur:
        erreur = f"Aucun équipement (ou plusieurs) ne correspond à « {recherche_modif} »"

    modeles_detail = db.lister_modeles_detail()
    return templates.TemplateResponse(request, "inventaire.html", {
        "utilisateur": admin,
        "modeles_detail": modeles_detail,
        "modeles_objets": db.lister_modeles_objets(),
        "acces_internet": internet.acces_internet(),
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
    automatique = id_modele is None
    if automatique:
        id_modele = await collecte.choisir_modele_integre(adresse, communaute_snmp, version_snmp)

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
    # Constructeur, capteurs, CPU et RAM : en tâche de fond, le parcours des capteurs peut être long
    collecte.lancer_identification([id_equipement], changer="TOUJOURS" if automatique else "JAMAIS")
    return RedirectResponse(f"/equipements/{id_equipement}/interfaces?identification=1", status_code=303)


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


@app.post("/equipements/{id_equipement}/objets/detecter")
async def detecter_objets_html(id_equipement: int, admin: Admin):
    """Relance la détection des objets surveillables sur cet équipement."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    await collecte.detecter_objets(equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#objets", status_code=303)


@app.get("/equipements/{id_equipement}/services", response_class=HTMLResponse)
async def choisir_services_html(request: Request, id_equipement: int, admin: Admin):
    """Choix des services à garder : mesures du modèle et objets détectés, groupés comme sur la fiche."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    groupes: dict[str, list] = {}
    for objet in db.lister_objets_detectes(id_equipement):
        titre = objet["table_nom"] if objet["table_nom"] and objet["table_nom"] != objet["objet"] else "Valeurs simples"
        groupes.setdefault(f"{titre} · {objet['mib']}", []).append(objet)
    return templates.TemplateResponse(request, "services.html", {
        "utilisateur": admin, "equipement": equipement, "groupes": groupes, "page_active": "inventaire",
        "metriques": db.metriques_du_modele(id_equipement),
    })


@app.post("/equipements/{id_equipement}/services")
async def enregistrer_services_html(request: Request, id_equipement: int, admin: Admin):
    """Garde les services cochés. Une mesure décochée n'est plus relevée ; un objet décoché n'est plus
    affiché ni relevé (son historique est effacé). Leurs seuils sont mis de côté, leurs alertes closes."""
    if db.obtenir_equipement(id_equipement) is None:
        return RedirectResponse(f"/inventaire?erreur={quote('Équipement introuvable')}", status_code=303)
    formulaire = await request.form()
    db.choisir_metriques_gardees(id_equipement, {int(v) for v in formulaire.getlist("garder_mesure") if str(v).isdigit()})
    db.choisir_objets_gardes(id_equipement, {int(v) for v in formulaire.getlist("garder") if str(v).isdigit()})
    collecte.oublier_echeances(id_equipement)
    collecte.reevaluer_seuils(id_equipement)
    return RedirectResponse(f"/equipements/{id_equipement}#mesures", status_code=303)


@app.get("/equipements/{id_equipement}/objets")
async def ancien_choix_objets_html(id_equipement: int, admin: Admin):
    return RedirectResponse(f"/equipements/{id_equipement}/services#objets", status_code=303)


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
                                  depuis: str | None = None, jusqu_a: str | None = None,
                                  agrege: bool = False):
    """Historique des relevés d'une métrique pour un équipement (et, si fournie, une
    interface). Query params : metrique (code, ex. "cpu"), interface_id, depuis, jusqu_a, agrege
    (ajoute les moyennes par heure du détail déjà purgé)."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    mesures = db.lister_mesures(id_equipement, metrique, interface_id, depuis, jusqu_a, agrege)
    return [dict(ligne) for ligne in mesures]


@app.get("/api/equipements/{id_equipement}/volumes")
async def volumes_equipement_api(id_equipement: int, utilisateur: Connecte,
                                 depuis: str | None = None, jusqu_a: str | None = None,
                                 agrege: bool = False):
    """Historique de chaque volume disque : {nom: [{horodatage, valeur, utilise, total}]}."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    series: dict[str, list[dict]] = {}
    for ligne in db.lister_volumes(id_equipement, depuis, jusqu_a, agrege):
        series.setdefault(ligne["nom"], []).append(
            {"horodatage": ligne["horodatage"], "valeur": ligne["valeur"], "utilise": ligne["utilise"], "total": ligne["total"]})
    return series


@app.get("/api/equipements/{id_equipement}/debit")
async def debit_equipement_api(id_equipement: int, metrique: Literal["if_in_octets", "if_out_octets"],
                                interface_id: int, utilisateur: Connecte,
                                depuis: str | None = None, jusqu_a: str | None = None,
                                agrege: bool = False):
    """Débit d'une interface en bits/s, calculé à partir des relevés bruts de ses compteurs."""
    interface = next((itf for itf in db.lister_interfaces(id_equipement) if itf["id"] == interface_id), None)
    if interface is None:
        return JSONResponse(status_code=404, content={"erreur": "Interface introuvable"})
    mesures = db.lister_mesures(id_equipement, metrique, interface_id, depuis, jusqu_a, agrege)
    return metriques.calculer_debit(mesures, interface["vitesse"])


@app.get("/api/equipements/{id_equipement}/objets/{objet_id}/mesures")
async def mesures_objet_api(id_equipement: int, objet_id: int, utilisateur: Connecte, agrege: bool = False):
    """Historique d'un objet détecté, par instance, pour ses graphiques."""
    series: dict[str, list[dict]] = {}
    for ligne in db.lister_mesures_objet(id_equipement, objet_id, agrege):
        series.setdefault(ligne["instance"], []).append({"horodatage": ligne["horodatage"], "valeur": ligne["valeur"]})
    return series


@app.get("/api/alertes")
async def lister_alertes_api(utilisateur: Connecte, statut: Literal["ACTIVE", "CLOTUREE"] | None = None):
    """Liste des alertes, éventuellement filtrée par statut."""
    return [dict(ligne) for ligne in db.lister_alertes(statut=statut)]


@app.get("/api/equipements/{id_equipement}/seuils")
async def seuils_equipement_api(id_equipement: int, utilisateur: Connecte):
    """Services gardés de l'équipement qui peuvent recevoir un seuil, avec leur unité et leur seuil
    (`origine` : propre ou defaut ; null si aucun)."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    return [{"cible": c["cle"], "libelle": c["libelle"], "groupe": c["groupe"], "unite": c["profil"]["unite"],
             "nature": c["profil"]["nature"], "etats": c["profil"].get("etats"), "seuil": c["seuil"] and {
                 k: c["seuil"][k] for k in ("operateur", "avertissement", "critique", "actif", "origine")}}
            for c in collecte.cibles_de_seuil(id_equipement)]


@app.post("/api/equipements/{id_equipement}/seuils")
async def enregistrer_seuil_equipement_api(id_equipement: int, payload: SeuilEquipement, admin: Admin):
    """Enregistre le seuil d'un service de cet équipement."""
    erreur = _enregistrer_seuil(id_equipement, payload.cible, payload.operateur, payload.avertissement,
                                payload.critique, payload.actif)
    if erreur:
        return JSONResponse(status_code=400, content={"erreur": erreur})
    return {"statut": "ok"}


@app.delete("/api/equipements/{id_equipement}/seuils")
async def remettre_a_zero_seuils_api(id_equipement: int, admin: Admin):
    """Retire tous les seuils réglés sur cet équipement."""
    if db.obtenir_equipement(id_equipement) is None:
        return JSONResponse(status_code=404, content={"erreur": "Équipement introuvable"})
    db.supprimer_seuils_equipement(id_equipement)
    collecte.reevaluer_seuils(id_equipement)
    return {"statut": "ok"}


@app.delete("/api/equipements/{id_equipement}/seuils/{cible}")
async def supprimer_seuil_equipement_api(id_equipement: int, cible: str, admin: Admin):
    """Retire le seuil réglé : CPU, RAM et disque reviennent à leur valeur par défaut."""
    trouvee = next((c for c in collecte.cibles_de_seuil(id_equipement) if c["cle"] == cible), None)
    if trouvee is None:
        return JSONResponse(status_code=404, content={"erreur": "Service introuvable"})
    db.supprimer_seuil(id_equipement, trouvee["metrique_id"], trouvee["objet_id"])
    collecte.reevaluer_seuils(id_equipement)
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
    id_modele = payload.modele_supervision_id
    automatique = id_modele is None
    if automatique:
        id_modele = await collecte.choisir_modele_integre(payload.adresse, payload.communaute_snmp, payload.version_snmp)

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
    collecte.lancer_identification([id_equipement], changer="TOUJOURS" if automatique else "JAMAIS")
    return {"id": id_equipement, "modele_supervision_id": id_modele, "identification_en_cours": True}
