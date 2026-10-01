"""Point d'entrée de l'application FastAPI.

- Pages HTML (Jinja2)  : /
- API JSON             : /api/...
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config
from app.snmp_client import SnmpError, lire_infos_systeme

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Supervision SNMP", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    infos, erreur = await _interroger_cible()
    return templates.TemplateResponse(request, "index.html", {
        "cible": f"{config.SNMP_TARGET}:{config.SNMP_PORT}",
        "communaute": config.SNMP_COMMUNITY,
        "infos": infos,
        "erreur": erreur,
    })


# ---------------------------------------------------------------- API JSON
@app.get("/api/sante")
async def sante():
    """Vérifie simplement que l'application répond."""
    return {"statut": "ok", "version": app.version}


@app.get("/api/equipement-test")
async def equipement_test():
    """Groupe "system" de l'équipement de test, au format JSON."""
    infos, erreur = await _interroger_cible()
    if erreur:
        return JSONResponse(status_code=504, content={"cible": config.SNMP_TARGET, "erreur": erreur})
    return {"cible": config.SNMP_TARGET, **infos}
