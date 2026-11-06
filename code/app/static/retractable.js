// Mémorise, dans ce navigateur, les blocs dépliés ou repliés. Un même bloc partage son état entre
// toutes les fiches d'équipement. Sans stockage disponible, chaque bloc garde son état par défaut.
(function () {
  const cle = (id) => "retractable:" + window.location.pathname.replace(/\d+/g, "#") + ":" + id;

  function lire(id) {
    try {
      return localStorage.getItem(cle(id));
    } catch {
      return null;
    }
  }

  function ecrire(id, ouvert) {
    try {
      localStorage.setItem(cle(id), ouvert ? "1" : "0");
    } catch {
      // stockage indisponible : l'état n'est simplement pas mémorisé
    }
  }

  document.querySelectorAll("details[id]").forEach((bloc) => {
    const etat = lire(bloc.id);
    if (etat !== null && !bloc.hasAttribute("data-filtre")) bloc.open = etat === "1";
  });

  // Après un enregistrement, la page revient sur son bloc (#reglages…) : il doit être visible
  if (window.location.hash) {
    const cible = document.getElementById(window.location.hash.slice(1));
    const bloc = cible && cible.closest("details");
    if (bloc) {
      bloc.open = true;
      cible.scrollIntoView();
    }
  }

  document.addEventListener("click", (evenement) => {
    const titre = evenement.target.closest("summary");
    if (!titre || evenement.target.closest("a, button, input, select")) return;
    const bloc = titre.parentElement;
    if (bloc.id) setTimeout(() => ecrire(bloc.id, bloc.open), 0); // le navigateur bascule après le clic
  });
})();
