// Actualise périodiquement les zones marquées data-rafraichir, sans recharger la page :
// les formulaires en cours de saisie, placés hors de ces zones, ne sont donc jamais effacés.
(function () {
  const periode = Number(document.currentScript.dataset.periode) * 1000;

  async function actualiser() {
    if (document.hidden) return;
    let reponse;
    try {
      reponse = await fetch(window.location.href, { headers: { Accept: "text/html" } });
    } catch {
      return; // serveur momentanément injoignable : on réessaiera au prochain passage
    }
    if (reponse.redirected) {
      window.location.reload(); // session expirée ou équipement supprimé
      return;
    }
    if (!reponse.ok) return;

    const page = new DOMParser().parseFromString(await reponse.text(), "text/html");
    document.querySelectorAll("[data-rafraichir]").forEach((zone) => {
      const nouvelle = page.getElementById(zone.id);
      if (!nouvelle) return;
      // Le serveur ne connaît pas les blocs dépliés par l'utilisateur : on reporte leur état
      [zone, ...zone.querySelectorAll("details[id]")].forEach((bloc) => {
        const equivalent = bloc.id === zone.id ? nouvelle : nouvelle.querySelector("#" + CSS.escape(bloc.id));
        if (bloc.tagName === "DETAILS" && equivalent) equivalent.open = bloc.open;
      });
      if (nouvelle.outerHTML !== zone.outerHTML) {
        zone.replaceWith(document.importNode(nouvelle, true));
      }
    });
  }

  if (periode > 0) setInterval(actualiser, periode);
})();
