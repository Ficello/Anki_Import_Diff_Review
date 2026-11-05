# Configuration — Revue Différentielle des Imports (Anki Import Diff)

Ce module fournit un comparateur visuel des modifications de cartes lors de l'importation de paquets ou de la synchronisation collaborative via AnkiCollab.

## Options de comparaison et d'interception

- `visual_diff_enabled` (booléen) : active la fenêtre de revue différentielle lors de l'importation de fichiers paquets standards (`.apkg`, `.colpkg`).
- `ankicollab_diff_enabled` (booléen) : intercepte les mises à jour distantes issues d'AnkiCollab et affiche les différences avant validation et intégration.
- `only_changed_fields` (booléen) : restreint l'affichage comparatif aux seuls champs ayant subi une modification effective, masquant les champs identiques.
- `learned_cards_only` (booléen) : filtre la revue différentielle pour ne soumettre à vérification que les cartes déjà en cours d'apprentissage (ignorant les nouvelles cartes non encore étudiées). Activé par défaut (true).
- `browser_diff_state` (objet) : persistance automatique des dimensions graphiques de l'interface :
  - `main_splitter_sizes` : répartition des largeurs entre la liste des cartes et le panneau de comparaison.
  - `editor_diff_sizes` : répartition des hauteurs entre le texte original et le texte révisé.
