# ETRS011-Projet_developpement-ALEXANDRE_TOINET
**Système de supervision d'un parc de matériels réseau via le protocole SNMP**
*Projet de Master 2 TRI réalisé dans le cadre de la ressource ETRS011.*
 
---
 
## 📖 À propos
 
Ce projet vise à développer un outil web permettant à un technicien ou ingénieur réseau de **superviser un parc d'équipements réseau** (routeurs, switchs, etc.) via le protocole **SNMP**.
 
Fonctions principales visées par le socle minimal :
- **Gestion de l'inventaire** des équipements supervisés (ajout, modification, suppression ou archivage, groupes).
- **Supervision du parc** via un tableau de bord et une fiche détaillée par équipement (statut, métriques courantes).
- **Historique des mesures** collectées (CPU, mémoire, trafic, etc.) sous forme de courbes temporelles.
- **Gestion des alertes** sur dépassement de seuils configurables.
L'outil est pensé pour un usage local, sur un unique poste de travail (pas de haute disponibilité ni de synchronisation multi-instances), conformément au périmètre défini dans le cahier des charges.
 
---
 
## ✅ Statut du projet — version 1.0
 
**Version 1.0 : le développement est terminé, l'application est prête à être présentée.**

L'application est fonctionnelle, avec une interface à l'identité visuelle propre (navigation latérale, fond clair, couleur signature vert anis) : inventaire des équipements (avec groupes, supprimables depuis le tableau de bord une fois vides, et plusieurs modèles de supervision sélectionnables), création de modèles de supervision personnalisés depuis l'interface (métriques ajoutées une par une avec des OID saisis à la main, testables en direct sur un équipement, et affichées automatiquement en graphique sur la fiche équipement), collecte SNMP automatique et périodique, tableau de bord (regroupement par groupe, répartition des statuts), fiche détaillée par équipement (identité, interfaces et volumes de stockage détectés automatiquement et activables/désactivables individuellement, débit réseau et taux d'utilisation par interface calculés à partir des compteurs 64 bits quand l'équipement les fournit, graphiques avec plage de dates réglable, paramètres de collecte). Les seuils globaux (CPU, mémoire, stockage, utilisation d'interface) déclenchent des alertes visuelles et un statut « dégradé » expliqué directement sur la fiche de l'équipement concerné ; une interface supervisée qui tombe dégrade l'équipement de la même façon. Une page Logs centralise l'historique des statuts, des erreurs de collecte et des alertes, filtrable par type, par équipement et par période. La durée de conservation des mesures est réglable par équipement (30 jours maximum) : elles sont gardées en détail sur les 15 derniers jours, puis agrégées par demi-heure (moyenne, minimum, maximum) avant d'être purgées ; les changements de statut et les alertes sont conservés 365 jours, les erreurs de collecte 30 jours. Un équipement retiré peut être supprimé avec tout son historique, ou archivé en conservant ses logs.

La relation parent-enfant entre équipements et la corrélation d'alertes par cause racine, envisagées dans le cahier des charges, n'ont finalement pas été retenues : pour être fiables, elles supposeraient de découvrir automatiquement ce qui est branché derrière chaque port (LLDP/CDP), ce qui dépasse le cadre du projet.

### 🖼️ Captures d'écran

Une visite complète de l'interface (tableau de bord, inventaire, modèles de supervision, alertes, logs, fiches équipement dans leurs différents états) est disponible dans le dossier **[Captures d'écran GUI](Captures%20d%27%C3%A9cran%20GUI)**, réalisée sur un parc de démonstration.

![Tableau de bord](Captures%20d%27%C3%A9cran%20GUI/02_tableau_de_bord_groupes_deplies.png)

![Fiche équipement](Captures%20d%27%C3%A9cran%20GUI/11_equipement_detail_DC1-FW-01.png)

### ▶️ Lancement

Depuis le dossier `code/`, avec [Docker Desktop](https://www.docker.com/products/docker-desktop/) démarré :

```bash
docker compose up --build
```

- Tableau de bord : http://localhost:8080
- Documentation de l'API (Swagger) : http://localhost:8080/docs

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
- Conserver une **trace des échanges avec l'IA** (ex: discussions, suggestions, décisions) : voir **[Utilisation de l'IA dans le projet](Documents/Utilisation%20de%20l%27IA.md)**.
- **Expliquer les raisons** ayant conduit à retenir ou rejeter certaines propositions.
- **Justifier certaines parties de code** retenues (choix algorithmiques, optimisations, etc.).
---
 
## **📌 Sommaire**

### **Documents clés**
- **[Cahier des charges - fonctionnel](Documents/Cahier%20des%20charges.pdf)**
- **[Cahier des charges - technologique](Documents/Specifications%20techniques.pdf)**
- **[SLIDE - Visualisation Globale](Documents/Slide%20powerpoint%20-%20Explications%20mi-parcours.pptx)**
- **[Planning](https://canva.link/12wkb8e5b6src8n)**

### **Modélisation**
- **[Diagramme de cas d'utilisation](Documents/Diagrammes/Diagramme_cas_utilisation.png)**
- **[Diagramme de classes](Documents/Diagrammes/Diagramme_classes.png)**

### **Application**
- **[Code source](code/)** (lancement : voir la section « Lancement » plus haut)
- **[Captures d'écran de l'interface](Captures%20d%27%C3%A9cran%20GUI)**

### **Utilisation de l'IA**
- **[Utilisation de l'IA dans le projet](Documents/Utilisation%20de%20l%27IA.md)**
---
 
## 👥 Auteurs
 
Projet réalisé en binôme par **Louis ALEXANDRE** et **Mattis TOINET**, étudiants en Master 2 TRI (Réseaux, Télécommunication, Informatique) — Université Savoie Mont Blanc.
