#!/bin/sh
# Faux capteurs d'environnement pour la démo, servis par snmpd via la directive « pass »
# (cf. snmpd.conf). snmpd appelle ce script avec « -g OID » (GET) ou « -n OID » (GETNEXT,
# utilisé par un WALK) ; le script répond sur trois lignes : OID, type, valeur (rien si
# l'OID n'existe pas).
#
# Branche 1.3.6.1.4.1.8072.9999 = netSnmpPlaypen, réservée par Net-SNMP aux expérimentations.
#   .1.1.0  température du châssis (°C)
#   .1.2.0  vitesse du ventilateur 1 (tr/min)
#   .1.3.0  vitesse du ventilateur 2 (tr/min)
#   .1.4.0  consommation électrique (W)
#   .1.5.0  clients connectés
#   .1.6.0  état de l'alimentation (1 = normal, 2 = alerte)
#   .2.<ifIndex>  taux de perte de paquets par interface (‰, pour mille)
#
# Les valeurs suivent un cycle lent (sinus sur l'heure courante) plus un peu de bruit : les
# courbes bougent pendant une démo, et les ventilateurs accélèrent quand la température monte.

BASE=".1.3.6.1.4.1.8072.9999"

valeurs() {
  awk -v t="$(date +%s)" -v b="$BASE" 'BEGIN {
    srand(t)
    cycle = sin(2 * 3.14159 * t / 600)               # période de 10 minutes
    temp = 47 + 7 * cycle + rand() * 1.5
    printf "%s.1.1.0 gauge %d\n", b, temp
    printf "%s.1.2.0 gauge %d\n", b, 2400 + (temp - 38) * 110 + rand() * 60
    printf "%s.1.3.0 gauge %d\n", b, 2350 + (temp - 38) * 105 + rand() * 60
    printf "%s.1.4.0 gauge %d\n", b, 92 + 18 * sin(2 * 3.14159 * t / 900) + rand() * 4
    printf "%s.1.5.0 gauge %d\n", b, 20 + 12 * sin(2 * 3.14159 * t / 1200) + rand() * 3
    printf "%s.1.6.0 integer %d\n", b, (temp > 53 ? 2 : 1)
    printf "%s.2.1 gauge %d\n", b, rand() * 2
    printf "%s.2.2 gauge %d\n", b, 4 + 3 * cycle + rand() * 3
  }'
}

case "$1" in
  -g)
    valeurs | awk -v o="$2" '$1 == o { print $1; print $2; print $3 }'
    ;;
  -n)
    # Premier OID de la liste strictement « après » celui demandé (comparaison numérique
    # composante par composante, comme le fait SNMP).
    valeurs | awk -v o="$2" '
      function apres(a, b,   x, y, n, m, i) {
        n = split(a, x, "."); m = split(b, y, ".")
        for (i = 1; i <= n && i <= m; i++) {
          if (x[i] + 0 > y[i] + 0) return 1
          if (x[i] + 0 < y[i] + 0) return 0
        }
        return n > m
      }
      !trouve && apres($1, o) { print $1; print $2; print $3; trouve = 1 }'
    ;;
esac
