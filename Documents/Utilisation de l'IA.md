# Utilisation de l'intelligence artificielle dans le projet

*Projet ETRS011 — Super vision, logiciel de supervision réseau SNMP — Louis ALEXANDRE et Mattis TOINET*

Ce document répond à la demande de garder une trace de notre usage de l'IA, et d'expliquer quelles propositions nous avons retenues ou rejetées, et pourquoi.

L'outil utilisé est **Claude** (Anthropic) : en conversation pour la phase d'étude et de documentation, puis via **Claude Code** dans VS Code pendant le développement, où l'assistant a accès au dépôt et peut lire, écrire et exécuter le code.

En résumé : **nous avons défini ce que fait l'application et comment elle doit se comporter, et nous avons pris toutes les décisions. L'IA a servi d'outil de recherche, de relecture, et a produit une partie du code à partir de nos spécifications.** Nous avons relu, testé sur du matériel réel et corrigé la trajectoire à chaque étape, et nous sommes en mesure d'expliquer chaque partie de l'application.

---

## 1. Étude de l'existant et choix techniques

Nous avons demandé à l'IA de **synthétiser et comparer** les architectures possibles pour ce type d'outil, et de résumer le fonctionnement des solutions existantes (Zabbix, Nagios, LibreNMS, PRTG, Centreon), en confrontant plusieurs sources. Le but était de gagner du temps sur la recherche documentaire.

**Les décisions sont les nôtres**, prises au regard des contraintes du projet (32 h, binôme, profil réseau plutôt que développement) :

- **FastAPI + pages HTML générées côté serveur (Jinja2) + un peu de JavaScript**, plutôt qu'une application monopage (React, Vue…), jugée surdimensionnée pour le temps disponible.
- **SQLite avec du SQL écrit à la main, sans ORM** : SQLAlchemy et Alembic, proposés au départ, ont été **écartés** car trop complexes à maîtriser et à justifier dans le temps imparti. Nous voulions comprendre chaque requête.
- **PySNMP**, **APScheduler** dans le même processus que l'application, et **Docker** pour un lancement identique sur nos deux postes.
- **SNMP v2c uniquement**, **aucune authentification** (outil local, réseau de confiance) : choix de périmètre assumés dans le cahier des charges.

## 2. Conception et documentation

Le cahier des charges, les spécifications, le modèle de données et les règles métier (disponibilité après N échecs consécutifs, trous dans les courbes plutôt que des zéros, débit calculé à partir de deux relevés de compteur, conservation des données…) viennent de notre analyse du besoin.

L'IA nous a aidés à **mettre en forme** les diagrammes UML et, surtout, à **détecter nos erreurs**. Par exemple, après avoir réalisé le diagramme de cas d'utilisation, nous avons demandé à l'IA de le confronter au code afin d'identifier des potentielles incohérences que nous avons ensuite corrigé en conséquence. Même démarche pour le diagramme de classes, recalé sur la base de données finale.

## 3. Interface (front-end)

Nous avons **délégué entièrement l'écriture du HTML et du CSS** à l'IA, et c'est un choix assumé : la valeur ajoutée d'un ingénieur n'est plus, selon nous, de passer des heures sur la mise en forme d'une page alors qu'un outil le fait plus vite et mieux.

**La conception de l'interface vient en revanche de nous** : l'organisation des pages et des menus, ce que chaque écran doit montrer, la charte graphique (barre latérale sombre, fond clair, couleur signature vert anis) et des choix d'ergonomie (par exemple, retrouver un équipement par son nom ou son adresse IP plutôt que dans une longue liste déroulante).

## 4. Logique applicative (back-end)

Pour le développement de la logique applicative, nous nous sommes servis de Claude Code comme d'un développeur assistant. Concrètement, nous définissions et formalisions nous-mêmes chaque fonctionnalité : son objectif, son fonctionnement attendu, les règles à respecter, les tables concernées ainsi que les différents cas particuliers. Une fois ce fonctionnement clairement établi, nous pouvions solliciter l'IA pour obtenir de l'aide sur la syntaxe Python, comprendre certaines particularités du langage, nous accompagner dans l'implémentation d'une fonction ou encore nous conseiller sur le choix des modules et bibliothèques adaptés (PySNMP en mode asyncio, APScheduler, etc.). Son utilisation s'apparentait finalement à celle d'une documentation technique ou d'une recherche sur Internet : un moyen d'obtenir rapidement des réponses lorsqu'on rencontrait une difficulté technique, sans pour autant lui déléguer la conception ou la réalisation du projet.

**Comment nous gardions la main sur ce que l'IA était amené à produire :**
- nous relisions chaque modification et demandions des explications sur ce que nous ne comprenions pas du premier coup ;
- nous refusions ou faisions reprendre les propositions qui ne respectaient pas nos choix ou qui étaient inutilement complexes ;
- nous testions chaque fonctionnalité sur des agents SNMP de test (équipements simulés sous Docker), y compris en simulant des pannes pour vérifier les changements de statut et les alertes.

## 5. Notre regard sur cet usage

L'IA nous a fait gagner beaucoup de temps sur la recherche, la rédaction et l'écriture du code. Pour autant, elle **n'a pas remplacé notre jugement et nos connaissances techniques** lié au métié d'ingénieur systèmes et réseaux. Elle s'est trompée à plusieurs reprises : bugs introduits par ses propres modifications, fonctionnalités ajoutées sans qu'on les demande, que nous avons fait retirer. Tester sur du matériel réel et relire chaque proposition a été indispensable.

Nous pensons que c'est ainsi que l'ingénieur travaille désormais : il définit le besoin, l'architecture et les règles, il arbitre entre les solutions, il vérifie, et il délègue une partie de l'écriture du code à l'outil.
