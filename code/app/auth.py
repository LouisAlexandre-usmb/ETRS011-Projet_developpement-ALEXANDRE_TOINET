"""Connexion / déconnexion et contrôle d'accès.

- Pages      : GET/POST /login, POST /logout
- Dépendances à placer sur les routes :
    Depends(utilisateur_courant)  -> il faut être connecté (admin ou lecteur)
    Depends(exiger_admin)         -> il faut être admin
- Un visiteur non connecté est redirigé vers /login (pages) ou reçoit un 401 (API).
"""
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.securite import (
    HASH_LEURRE, effacer_echecs, noter_echec, secondes_avant_deblocage, verifier_mot_de_passe,
)
from app.utilisateurs import Utilisateur, trouver_utilisateur

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
router = APIRouter()


class NonConnecte(Exception):
    """Aucune session valide."""


class AccesRefuse(Exception):
    """Connecté, mais rôle insuffisant."""


# ---------------------------------------------------------------- dépendances
def utilisateur_courant(request: Request) -> Utilisateur:
    login = request.session.get("login")
    utilisateur = trouver_utilisateur(login) if login else None
    if utilisateur is None:  # pas de session, ou compte supprimé depuis
        request.session.clear()
        raise NonConnecte
    return utilisateur


def exiger_admin(utilisateur: Annotated[Utilisateur, Depends(utilisateur_courant)]) -> Utilisateur:
    if not utilisateur.est_admin:
        raise AccesRefuse
    return utilisateur


# --------------------------------------------------- gestionnaires d'erreurs
def _est_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


async def gerer_non_connecte(request: Request, exc: NonConnecte):
    if _est_api(request):
        return JSONResponse(status_code=401, content={"erreur": "authentification requise"})
    cible = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?suivant={quote(cible)}", status_code=303)


async def gerer_acces_refuse(request: Request, exc: AccesRefuse):
    if _est_api(request):
        return JSONResponse(status_code=403, content={"erreur": "droits administrateur requis"})
    return templates.TemplateResponse(request, "403.html", {
        "utilisateur": utilisateur_courant(request),
    }, status_code=403)


# ---------------------------------------------------------------- pages
def _cible_sure(url: str) -> str:
    """N'accepte qu'un chemin local, pour ne pas rediriger vers un autre site."""
    if url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def page_connexion(request: Request, suivant: str = "/"):
    if request.session.get("login"):
        return RedirectResponse(_cible_sure(suivant), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"suivant": _cible_sure(suivant)})


@router.post("/login", response_class=HTMLResponse)
async def connexion(
    request: Request,
    login: Annotated[str, Form()],
    mot_de_passe: Annotated[str, Form()],
    suivant: Annotated[str, Form()] = "/",
):
    cle = request.client.host if request.client else "inconnu"
    contexte = {"suivant": _cible_sure(suivant), "login": login}

    attente = secondes_avant_deblocage(cle)
    if attente:
        contexte["erreur"] = f"Trop de tentatives. Réessayez dans {attente} s."
        return templates.TemplateResponse(request, "login.html", contexte, status_code=429)

    utilisateur = trouver_utilisateur(login)
    mot_de_passe_ok = verifier_mot_de_passe(
        utilisateur.hash_mdp if utilisateur else HASH_LEURRE, mot_de_passe,
    )
    if utilisateur is None or not mot_de_passe_ok:
        noter_echec(cle)
        # Message volontairement vague : on ne dit pas si c'est le login ou le mot de passe
        contexte["erreur"] = "Identifiant ou mot de passe incorrect."
        return templates.TemplateResponse(request, "login.html", contexte, status_code=401)

    effacer_echecs(cle)
    request.session.clear()  # repart d'une session neuve
    request.session["login"] = utilisateur.login
    return RedirectResponse(contexte["suivant"], status_code=303)


@router.post("/logout")
async def deconnexion(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
