# Cahier des charges — Logiciel de supervision réseau
**M2 TRI - Université Savoie Mont Blanc**

---
## **Ressource ETRS011 — Module de développement logiciel (32 heures)**
**Master TRI — Réseaux, Télécommunications, Informatique**
**Université Savoie Mont Blanc — Campus du Bourget-du-Lac**

**Rédigé par :** Louis ALEXANDRE & Mattis TOINET

---

## **Suivi des versions**

Ce document est un document de travail, amené à être révisé au fil de l'avancement du projet.



| **Version** | **Date** | **Auteur(s)** | **Description** |
|-------------|----------|---------------|-----------------|
| 1.1 | 18/09/2026 | Louis ALEXANDRE & Mattis TOINET | Première version de travail — trame et premier jet de contenu, à compléter. |
| 1.2 | 23/09/2026 | Louis ALEXANDRE | Rédaction de la partie 5. Parcours de la donnée. |
| 1.3 | 24/09/2026 | Mattis TOINET | Réfection complète du document + publication GitHub. |

---

## **Sommaire**
1. [Objet du document](#1-objet-du-document)
2. [Contexte et problématique](#2-contexte-et-problématique)
3. [Objectifs du projet](#3-objectifs-du-projet)
4. [Périmètre fonctionnel](#4-périmètre-fonctionnel)
   - [4.1 Fonctionnalités incluses — version minimale viable (MVP)](#41-fonctionnalités-incluses--version-minimale-viable-mvp)
   - [4.2 Perspectives d'évolution](#42-perspectives-dévolution)
   - [4.3 Exclusions explicites (hors périmètre)](#43-exclusions-explicites-hors-périmètre)
5. [Parcours de la donnée](#5-parcours-de-la-donnée)
   - [5.1 Vue d'ensemble du parcours](#51-vue-densemble-du-parcours)
   - [5.2 Détail du parcours, étape par étape](#52-détail-du-parcours-étape-par-étape)
   - [5.3 Caractéristiques transverses du flux](#53-caractéristiques-transverses-du-flux)
   - [5.4 Préservation et intégrité de la donnée](#54-préservation-et-intégrité-de-la-donnée)
   - [5.5 Gestion des anomalies et des erreurs de collecte](#55-gestion-des-anomalies-et-des-erreurs-de-collecte)
   - [5.6 Politique de conservation et de suppression](#56-politique-de-conservation-et-de-suppression)
   - [5.7 Accessibilité de la donnée](#57-accessibilité-de-la-donnée)
6. [Profils utilisateurs et besoins](#6-profils-utilisateurs-et-besoins)
   - [6.1 Profils identifiés](#61-profils-identifiés)
   - [6.2 Besoins et niveau technique](#62-besoins-et-niveau-technique)
7. [Besoins métiers et parcours utilisateurs](#7-besoins-métiers-et-parcours-utilisateurs)
9. [Planning et jalons](#9-planning-et-jalons)

---

---

## **1. Objet du document**
Ce document constitue le **cahier des charges** du projet réalisé dans le cadre de la ressource **ETRS011** (module de développement logiciel, 32 heures), en binôme, au sein du **Master TRI** (Réseaux, Télécommunications, Informatique) de l'**Université Savoie Mont Blanc**.

**Objectif** :
Définir clairement le besoin auquel doit répondre le logiciel :
- Le problème adressé,
- Les objectifs poursuivis,
- Le périmètre fonctionnel visé,
- Les utilisateurs concernés.

*Les choix techniques (architecture, technologies) relèvent d'une phase de conception distincte et seront documentés séparément.*

---

---

## **2. Contexte et problématique**
Le suivi de l'état de fonctionnement d'un parc d'équipements réseau (commutateurs, routeurs, pare-feux, serveurs...) est une **nécessité opérationnelle** pour toute infrastructure informatique.
**Problème** :
En l'absence d'outil de supervision, une panne ou une dégradation de performance n'est détectée que lorsque ses effets deviennent visibles pour les utilisateurs finaux (perte de connectivité, ralentissement, services indisponibles), ce qui retarde la détection et l'intervention.

### **Solutions existantes**
De nombreuses solutions de supervision sont disponibles :
- **Open-source** : Zabbix, Nagios, LibreNMS.
- **Commerciales** : PRTG, Centreon.
Ces outils sont matures, riches fonctionnellement, et couvrent des périmètres larges (supervision réseau, applicative, système, sécurité...).

### **Positionnement du projet**
Le projet **ne vise pas** à concurrencer ces solutions, mais répond à un **double objectif** :
1. **Pédagogique** :
   Comprendre en profondeur le fonctionnement d'un outil de supervision réseau en le développant soi-même (protocole SNMP, architecture manager/agent, collecte, historisation et restitution de métriques).
2. **Pratique et ciblé** :
   Proposer un outil **centré sur la supervision des équipements réseau** (cœur de métier des membres du binôme, ingénieurs réseau en apprentissage), plutôt qu'une supervision généraliste.

**Innovation** :
Certaines fonctionnalités du socle fonctionnel ont été pensées pour constituer une **différenciation ponctuelle** par rapport aux outils existants.

---

---

## **3. Objectifs du projet**
**Objectif général** :
Développer un **outil web** permettant de superviser un parc d'équipements réseau via le **protocole SNMP**, assurant :
- La collecte des données,
- Leur historisation,
- Leur restitution visuelle,
au sein d'une **interface unique** intégrant également les fonctions d'administration du parc supervisé.

### **Fonctionnalités clés**
Le logiciel devra permettre de :
- Constituer et administrer un **inventaire des équipements réseau** à superviser.
- Interroger périodiquement ces équipements en **SNMP** pour en extraire des indicateurs de **disponibilité et de performance**.
- Conserver un **historique** de ces indicateurs dans le temps.
- Présenter l'état du parc de façon **synthétique**, et le détail de chaque équipement, via une **interface web unique**.
- Signaler visuellement les **anomalies détectées** (équipement inaccessible, seuil de performance dépassé).

---

---

## **4. Périmètre fonctionnel**

---
### **4.1 Fonctionnalités incluses — version minimale viable (MVP)**

#### **🔹 Gestion de l'inventaire des équipements**
Fonctionnalités pour déclarer, organiser et maintenir à jour la liste des équipements supervisés.

 |  **Fonctionnalité**          | **Description**                                                                                     |
 |-----------------------------|-----------------------------------------------------------------------------------------------------|
 | **Ajout d'un équipement**   | Nom/alias, adresse IP (ou nom d'hôte), description libre, paramètres d'accès SNMP (version, communauté), intervalle de polling spécifique. |
 | **Test de connectivité**   | Requête SNMP simple (ex: lecture de `sysDescr`) pour vérifier la configuration avant intégration au cycle de supervision. |
 | **Modification**            | Changement d'IP, de communauté, de description, etc.                                              |
 | **Suppression**             | Avec confirmation explicite (le devenir de l'historique est traité dans la [partie 5.6](#56-politique-de-conservation-et-de-suppression)). |
 | **Relations topologiques** | Définition de relations parent-enfant entre équipements pour la corrélation d'alertes.          |
 | **Regroupement**            | Étiquettes simples (par site ou type) pour faciliter le filtrage dans le tableau de bord.         |
 | **Recherche et filtrage**   | Par nom, statut ou groupe.                                                                         |

---

#### **🔹 Moteur de collecte périodique (polling SNMP)**
Module interrogeant automatiquement les équipements déclarés pour extraire leur état et leurs indicateurs.



<custom-element data-json="%7B%22type%22%3A%22table-metadata%22%2C%22attributes%22%3A%7B%22title%22%3A%22Moteur%20de%20collecte%22%7D%7D" />
 | **Fonctionnalité** | **Description** |
 |--------------------|----------------|
 | **Interrogation périodique** | Selon un intervalle configurable (valeur par défaut pour tout le parc). |
 | **Métriques standard** | Disponibilité, uptime, charge CPU, utilisation mémoire, compteurs de trafic entrant/sortant par interface. |
 | **Métriques dérivées** | Calcul du débit d'une interface par différence entre deux relevés successifs. |
 | **Détection de disponibilité** | Basée sur plusieurs échecs consécutifs pour limiter les faux positifs. |
 | **Exécution indépendante** | Un équipement lent ne retarde pas la collecte sur le reste du parc. |
 | **Journalisation des échecs** | Équipement injoignable, erreur d'authentification SNMP, indicateur non supporté. |

---

#### **🔹 Tableau de bord global**
Vue principale donnant un **état de santé global du parc** en un coup d'œil.



<custom-element data-json="%7B%22type%22%3A%22table-metadata%22%2C%22attributes%22%3A%7B%22title%22%3A%22Tableau%20de%20bord%22%7D%7D" />
 | **Fonctionnalité** | **Description** |
 |--------------------|----------------|
 | **Liste/grille des équipements** | Chaque équipement est associé à un indicateur visuel de statut (opérationnel, dégradé, indisponible, en attente). |
 | **Compteurs de synthèse** | Nombre total d'équipements, nombre en anomalie, nombre indisponibles. |
 | **Tri et filtrage** | Par statut, groupe ou nom. |
 | **Accès direct** | Depuis chaque équipement listé, accès à sa fiche détaillée. |
 | **Rafraîchissement automatique** | Vision proche du temps réel sans action manuelle. |

---
#### **🔹 Fiche détaillée par équipement**
Page rassemblant toutes les informations relatives à un équipement.



<custom-element data-json="%7B%22type%22%3A%22table-metadata%22%2C%22attributes%22%3A%7B%22title%22%3A%22Fiche%20d%C3%A9taill%C3%A9e%22%7D%7D" />
 | **Fonctionnalité** | **Description** |
 |--------------------|----------------|
 | **Informations d'identité** | Nom, adresse IP, description, groupe, relations parent/enfant. |
 | **Statut actuel et historique** | Chronologie des changements d'état (disponible/indisponible). |
 | **Graphiques d'évolution** | CPU, mémoire, trafic par interface, avec sélection de la période (dernière heure, 24h, 7 jours). |
 | **Liste des interfaces réseau** | Avec leur statut individuel si disponible via SNMP. |
 | **Historique des alertes** | Alertes déclenchées pour cet équipement. |

---
#### **🔹 Alertes visuelles**
Mise en évidence des situations nécessitant l'attention de l'utilisateur.



<custom-element data-json="%7B%22type%22%3A%22table-metadata%22%2C%22attributes%22%3A%7B%22title%22%3A%22Alertes%20visuelles%22%7D%7D" />
 | **Fonctionnalité** | **Description** |
 |--------------------|----------------|
 | **Types d'anomalies** | Indisponibilité d'un équipement ou dépassement d'un seuil de performance (CPU, mémoire). |
 | **Seuils globaux** | Définis par défaut pour l'ensemble du parc (simplicité). |
 | **Liste consolidée** | Alertes actives consultables depuis le tableau de bord, avec horodatage. |
 | **Corrélation intelligente** | Évite les alertes redondantes pour les équipements enfants en cas de panne parent. |
 | **Alerte visuelle** | Strictement dans l'interface (pas de notification externe dans le MVP). |

---
#### **🔹 Fonctionnalités différenciantes (objectifs non garantis)**
1. **Corrélation intelligente d'alertes** :
   - Configuration de relations de dépendance topologique pour éviter les alertes redondantes.
2. **Découverte automatique de la topologie (auto-discovery)** :
   - Utilisation des protocoles **LLDP-MIB** et **CISCO-CDP-MIB** pour découvrir automatiquement les équipements connectés et construire une carte topologique interactive.

---
---
### **4.2 Perspectives d'évolution**
Fonctionnalités souhaitables pour une version future, mais **non incluses dans le MVP** :
1. Support du **protocole SNMPv3** (authentification et chiffrement).
2. Exécution de **scripts personnalisés** à distance sur les équipements supervisés.
3. **Gestion multi-utilisateur** avec droits d'accès différenciés (authentification, rôles, permissions).

---
---
### **4.3 Exclusions explicites (hors périmètre)**
Pour limiter les risques de dérive sur un projet de **32 heures**, les éléments suivants sont **exclus** :
- Supervision applicative ou système généraliste (centrage sur les équipements réseau).
- Haute disponibilité ou répartition de charge de l'outil de supervision.
- Hébergement en production sur une infrastructure dédiée (exécution prévue sur les postes personnels).
- Envoi de **notifications externes** (e-mail, SMS, ChatOps) au-delà de l'alerte visuelle.

*À compléter au fil de l'avancement du projet.*

---

---
## **5. Parcours de la donnée**
Cette section décrit le **parcours de la donnée** au sein de l'outil :
- Origine,
- Entrée,
- Traitement,
- Stockage,
- Utilisation,
- Archivage/suppression.

---
### **5.1 Vue d'ensemble du parcours**
```mermaid
flowchart TD
    A[Source] --> B[Entrée]
    B --> C[Contrôles]
    C --> D[Traitement]
    D --> E[Stockage]
    E --> F[Transmission]
    F --> G[Utilisation]
    G --> H[Archivage / Suppression]
