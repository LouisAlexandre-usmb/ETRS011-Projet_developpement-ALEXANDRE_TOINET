// Réglages communs des graphiques, à la charte « atelier » : polices et couleurs lues dans les variables
// CSS (une seule source), courbe principale en graphite avec remplissage vert anis, autres courbes en
// couleurs sobres. À charger après Chart.js. Sans Internet, Chart.js est absent : seule la palette existe.
(function () {
  const css = getComputedStyle(document.documentElement);
  const lire = (nom) => css.getPropertyValue(nom).trim();
  const palette = [lire("--graphite"), "#8A94A6", "#5B7BD5", "#C0793A", "#7A9E5F", "#9B6BB5", "#3A9EA5", "#A8A397"];
  const remplissage = "rgba(200, 242, 92, .35)";

  window.CHARTE = {
    // Couleurs des statuts, dans l'ordre : opérationnel, dégradé, indisponible, en attente
    statuts: [lire("--statut-ok"), lire("--statut-degrade"), lire("--statut-ko"), lire("--statut-attente")],
    palette,
    // Style d'une courbe : la première, remplie, est la courbe « principale »
    serie(rang, rempli = false) {
      const style = {
        borderColor: palette[rang % palette.length], borderWidth: 2, tension: .3,
        pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12,   // points cliquables sans encombrer la courbe
      };
      if (rempli) { style.backgroundColor = remplissage; style.fill = "origin"; }
      return style;
    },
    // Survol et clic sur le point le plus proche, sans viser exactement
    interaction: { mode: "nearest", intersect: false },
  };

  if (typeof Chart === "undefined") return;
  Chart.defaults.font.family = "'Hanken Grotesk', system-ui, sans-serif";
  Chart.defaults.color = lire("--sourdine");
  Chart.defaults.borderColor = "rgba(31, 29, 26, .07)";
})();
