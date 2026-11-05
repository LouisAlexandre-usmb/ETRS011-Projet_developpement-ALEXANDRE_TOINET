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

- **Disponible** : gestion de l'inventaire avec test de connectivité SNMP (v1 ou v2c), groupes et relations parent-enfant, modèles de supervision détectés automatiquement (générique, Cisco, Linux), tableau de bord, connexion avec les rôles `admin` et `lecteur`, collecte périodique du statut et des mesures, débit par interface, fiche équipement avec graphiques, choix des interfaces à surveiller, seuils d'avertissement et critiques par service (globaux ou propres à un équipement), alertes séparant les équipements injoignables (ping) des services en erreur (relevé SNMP), corrélation des pannes par cause racine.
- **Préparé en base, sans interface** : paramètres généraux.
- **À réaliser** : choix de la période des graphiques, purge de l'historique, recherche et filtres, rafraîchissement automatique, découverte de la topologie.

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
