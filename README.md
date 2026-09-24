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
 
Le projet est actuellement en phase de **cadrage / conception** : le cahier des charges fonctionnel et les spécifications techniques sont rédigés, le développement n'a pas encore démarré. Les instructions d'installation et de lancement (`docker compose up`, etc.) seront ajoutées ici dès que le socle applicatif existera.
 
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
- **[Cahier des charges - fonctionnel](https://github.com/LouisAlexandre-usmb/ETRS011-Projet_developpement-ALEXANDRE_TOINET/blob/main/Documents/Cahier%20des%20charge.docx)**
- **[Cahier des charges - technologique](https://github.com/LouisAlexandre-usmb/ETRS011-Projet_developpement-ALEXANDRE_TOINET/blob/main/Documents/Specifications_techniques.docx)**

- **[Planning](https://canva.link/12wkb8e5b6src8n)**
---
 
## 👥 Auteurs
 
Projet réalisé en binôme par **Louis ALEXANDRE** et **Mattis TOINET**, étudiants en Master 2 TRI (Réseaux, Télécommunication, Informatique) — Université Savoie Mont Blanc.
