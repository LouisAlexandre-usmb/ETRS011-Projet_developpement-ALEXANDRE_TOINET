# ETRS011-Projet_developpement-ALEXANDRE_TOINET
**Système de supervision d'un parc de matériels réseau via le protocole SNMP**
*Projet de Master 2 TRI réalisé dans le cadre de la ressource ETRS011.*
 
---
 
## 📖 À propos
 
Ce projet vise à développer un outil web permettant à un technicien ou ingénieur réseau de **superviser un parc d'équipements réseau** (routeurs, switchs, etc.) via le protocole **SNMP**.
 
Fonctions principales visées par le socle minimal :
- **Gestion de l'inventaire** des équipements supervisés (ajout, modification, suppression, relations parent-enfant).
- **Supervision du parc** via un tableau de bord et une fiche détaillée par équipement (statut, métriques courantes).
- **Historique des mesures** collectées (CPU, mémoire, trafic, etc.) sous forme de courbes temporelles.
- **Gestion des alertes** sur dépassement de seuils configurables.
L'outil est pensé pour un usage local, sur un unique poste de travail (pas de haute disponibilité ni de synchronisation multi-instances), conformément au périmètre défini dans le cahier des charges.
 
---
 
## 🚧 Statut du projet
 
Le projet est **en cours de développement**.

- **Disponible** : gestion de l'inventaire avec test de connectivité SNMP (v1 ou v2c), groupes (création et suppression) et relations parent-enfant, modèles intégrés (générique, hôte standard, Cisco, Aruba, QNAP, Synology, capteurs matériels, onduleur, imprimante), attribués automatiquement à l'ajout d'un équipement selon son constructeur reconnu, capteurs et objets détectés en listes et graphiques, capacité disque et mémoire en tailles (par volume), uptime en compteur, tableau de bord, connexion avec les rôles `admin` et `lecteur`, collecte périodique du statut et des mesures, débit par interface, fiche équipement avec graphiques, choix des interfaces à surveiller, choix des services à garder (mesures et objets détectés), seuils d'avertissement et critiques réglés sur chaque équipement pour chaque service gardé (supérieur à, inférieur à ou égal à, dans l'unité du service : %, Mb/s, minutes, °C, état…), alertes séparant les équipements injoignables (ping) des services en erreur (relevé SNMP) et des interfaces suivies passées « down » (physiques ou VLAN), corrélation des pannes par cause racine, onglet Logs (statuts, erreurs de collecte, alertes) avec recherche, filtres et pagination, santé du parc en camembert, recherche et filtres du tableau de bord, intervalle de polling, échecs avant alerte, conservation du détail et conservation agrégée réglables globalement et par équipement, agrégation en moyennes par heure du détail purgé (mesures, volumes, objets détectés) affichée sur la fiche avec « Historique agrégé ».
- **Préparé en base, sans interface** : délai de réponse SNMP et période de rafraîchissement.
- **À réaliser** : choix de la période des graphiques, découverte de la topologie.

### Modèles

Tous les modèles sont livrés avec l'application, dans [`code/app/modeles_integres.json`](code/app/modeles_integres.json). Ils fonctionnent sans Internet et ne se modifient pas depuis l'interface (**Gestion de l'inventaire → Modèles**).

- **Modèles génériques** : *Équipement générique* (disponibilité, uptime, trafic) et *Hôte standard* (CPU, mémoire sans le cache et disques par HOST-RESOURCES-MIB, ou UCD-SNMP-MIB en secours).
- **Modèles des constructeurs** : *Switch Cisco* (CPU par CISCO-PROCESS-MIB, mémoire par CISCO-MEMORY-POOL-MIB), *Switch Aruba (HP ProCurve)* (STATISTICS-MIB, NETSWITCH-MIB), *NAS QNAP* et *NAS Synology* (HOST-RESOURCES-MIB), chacun avec ses capteurs : températures, ventilateurs, états des disques, du RAID, des alimentations…
- **Modèles d'objets génériques** : *Capteurs matériels* (ENTITY-SENSOR-MIB), *Onduleur* (UPS-MIB) et *Imprimante* (Printer-MIB).

À l'ajout d'un équipement, l'application reconnaît son constructeur grâce aux règles du fichier (début du `sysObjectID`, texte du `sysDescr`, lecture d'un OID), puis lui attribue le modèle qui fournit le plus d'indicateurs : celui du constructeur, sinon l'hôte standard, sinon le générique. Les capteurs d'un constructeur ne sont cherchés que sur ses équipements.

### Logs et santé du parc

- L'onglet **Logs** réunit les changements de statut, les erreurs de collecte et les alertes de tout le parc, du plus récent au plus ancien. On peut les filtrer par équipement, créneau de dates, catégorie (statut, erreur, avertissement, critique) et texte, avec 50 lignes par page.
- Le **tableau de bord** affiche la santé du parc : le nombre d'équipements opérationnels, dégradés, indisponibles et en attente, et leur répartition en camembert.

### Lancer le projet

Depuis le dossier `code/`, avec Docker :

```bash
docker compose up --build
```

L'application est disponible sur http://localhost:8080. Les comptes de développement sont `admin` / `admin` et `lecteur` / `lecteur` : ces valeurs, définies dans `docker-compose.yml`, sont à changer hors de vos postes. La documentation de l'API est sur http://localhost:8080/docs.

### Écarts avec les documents de conception

Le code a évolué depuis la rédaction des documents du dossier `Documents/`. En cas de différence, c'est cette section qui fait foi :

- **Authentification** : le cahier des charges, les spécifications (section 7) et le diagramme de cas d'utilisation décrivent une version initiale sans authentification. L'application comporte en réalité un écran de connexion et deux rôles.
- **Conteneurs** : les spécifications prévoient un conteneur unique. `docker-compose.yml` en lance un second, un agent SNMP de test qui simule un équipement réseau pendant le développement.
- **Schéma des tables** : la section 6.2 des spécifications reste à compléter. Le schéma actuel se trouve dans [`code/app/db.py`](code/app/db.py).
 
---
 
## **Objectifs du projet**
 
### **Objectifs de base**
1. **Étudier l'existant** :
   - Analyser les solutions existantes sur le marché sous différents angles :
     - Convivialité,
     - Déploiement,
     - Mise à jour logicielle, etc.
2. **Établir un cahier des charges détaillé** :
   - Définir les besoins, les fonctionnalités et les contraintes du projet.
3. **Développement** : 
   - a. **Établir une architecture précise et détaillée** : Modélisation du système.
   - b. **Réaliser et justifier les choix technologiques** : Sélection des outils, langages et frameworks.
   - c. **Établir un planning de travail** : Répartition des tâches et échéances.
### **Exigences supplémentaires**
Dans le cadre de ce développement, il sera également demandé de :
- Conserver une **trace des échanges avec l'IA** (ex: discussions, suggestions, décisions).
- **Expliquer les raisons** ayant conduit à retenir ou rejeter certaines propositions.
- **Justifier certaines parties de code** retenues (choix algorithmiques, optimisations, etc.).
---
 
## **📌 Sommaire**

### **Documents clés**
- **[Cahier des charges - fonctionnel](https://github.com/LouisAlexandre-usmb/ETRS011-Projet_developpement-ALEXANDRE_TOINET/blob/main/Documents/Cahier%20des%20charges.pdf)**
- **[Cahier des charges - technologique](https://github.com/LouisAlexandre-usmb/ETRS011-Projet_developpement-ALEXANDRE_TOINET/blob/main/Documents/Specifications%20techniques.pdf)**
- **[SLIDE - Visualisation Globale](https://github.com/LouisAlexandre-usmb/ETRS011-Projet_developpement-ALEXANDRE_TOINET/blob/3bf597d66aadaf42a046e3a4b6a27f496faeed67/Documents/Slide%20powerpoint%20-%20Explications%20mi-parcours.pptx)**

- **[Planning](https://canva.link/12wkb8e5b6src8n)**
---
 
## 👥 Auteurs
 
Projet réalisé en binôme par **Louis ALEXANDRE** et **Mattis TOINET**, étudiants en Master 2 TRI (Réseaux, Télécommunication, Informatique) — Université Savoie Mont Blanc.
