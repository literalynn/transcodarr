# DECISIONS.md — décisions d'implémentation

Journal des choix pris en autonomie quand le cahier des charges laissait une
ambiguïté ou qu'une contrainte était techniquement risquée (mode de travail,
point 3 du prompt de reprise). Les « arbitrages tranchés » validés par le
client ne sont pas répétés ici, seulement leur mise en œuvre quand elle
méritait une précision.

---

## 2026-07-12 — Injection MDCV/MaxCLL : mécanisme concret (arbitrage 1)

**Problème.** `hevc_metadata` (bsf FFmpeg) ne sait pas insérer de SEI
MDCV/CLL, et `av1_qsv` ne pose pas les metadata OBU depuis la CLI. Quand la
source porte déjà les side data (HDR10 natif), jellyfin-ffmpeg les fait
passer dans le bitstream via l'encodeur. Quand elle ne les porte PAS
(DV P5/P7 typiquement), il n'existe aucun chemin CLI fiable pour synthétiser
le SEI.

**Choix.** Étape post-encode systématique pour toute sortie HDR10 en MKV :
`mkvpropedit --edit track:v1 --set …` pose MasteringMetadata + MaxCLL/MaxFALL
au niveau conteneur Matroska (valeurs : source > RPU L6 via dovi_tool >
défauts sûrs loggés). ffprobe relit ces éléments comme side data du flux :
la validation §5.5.8 les vérifie uniformément pour HEVC et AV1. mkvtoolnix
est embarqué dans l'image. L'injection a lieu AVANT la validation, jamais
après.

**Alternative écartée.** Patcher jellyfin-ffmpeg pour exposer une option
`master_display` sur les encodeurs QSV : hors périmètre et fragile aux mises
à jour. Conséquence assumée : en conteneur MP4 + HDR sans metadata source,
l'injection conteneur n'existe pas → la validation échoue et l'original est
conservé (comportement sûr, documenté dans le README ; MKV est le défaut).

## 2026-07-12 — Repli P5 « hwdownload » implémenté par négociation de format

**Problème.** L'arbitrage 6 décrit le repli comme `hwdownload,format=p010le`.
Or si libplacebo négocie une sortie logicielle (ce qu'il fait quand l'aval
n'accepte pas de frames Vulkan), `hwdownload` échoue (« input must be hw »).

**Choix.** Variante de repli = `libplacebo=…:format=p010le` sans filtre
aval : la négociation FFmpeg produit des frames système p010le (download
interne à libplacebo), que `hevc_qsv`/`av1_qsv` remontent en VRAM — le
résultat est exactement la sémantique « hwdownload » voulue. La chaîne
zéro-copie (`hwmap=derive_device=qsv,format=qsv`, spec §5.3.1) reste la
variante principale, testée au démarrage par capabilities.py ; échec → repli.

## 2026-07-12 — DV profil 8, compat_id 6 traité comme 8.1

`bl_compat_id = 6` (« Blu-ray ») désigne une couche de base HDR10 valide au
sens Dolby. Même traitement que 8.1 : réencodage sans RPU, flags BT.2020/PQ,
MDCV/CLL réinjectés. Tout autre compat_id non listé (0, 3, 5…) reste
`skipped_policy` avec motif explicite — jamais de devinette (règle du §5.3.1).

## 2026-07-12 — Filtre de date : filtre DUR, pas simple priorité

§4/§7.5 disent « ne traiter QUE les fichiers ajoutés après cette date » ;
§5.2 mentionne ces fichiers « en premier » dans l'ordonnancement. Contradiction
tranchée au profit du filtre dur : les fichiers antérieurs passent en
`ignored` (motif visible dans l'UI, réversibles via « Forcer le réencodage »
ou en vidant le filtre). L'ordonnancement §5.2 s'applique au reste.

## 2026-07-12 — Job annulé → fichier remis en `pending`

Le cahier des charges ne fixe pas l'état du fichier après annulation. Choix :
`pending` (neutre) — le fichier resterait requalifié `queued` au scan suivant
si sa situation le justifie toujours ; l'utilisateur qui veut l'exclure
définitivement utilise « Ignorer ». Alternative écartée : `failed`
(consommerait les retries pour une action volontaire).

## 2026-07-12 — Dry-run : fichier marqué `done` en base

En dry-run réussi, `media_files.status = done` (historique/stats complets,
§9.4) mais l'ORIGINAL ne porte pas de tag conteneur. Au passage en réel, le
scan suivant re-queue naturellement ces fichiers (« tag absent », §5.7) :
aucun état spécial nécessaire, et le badge DRY RUN de l'UI lève l'ambiguïté.

## 2026-07-12 — Nouvelle bibliothèque : règles seedées sur l'échelle « Films »

`POST /libraries` crée d'office les 24 règles (HEVC+AV1 × 6 buckets × SDR/HDR)
avec l'échelle Films du §4, pour qu'une bibliothèque soit fonctionnelle dès sa
création (le scanner marquerait `failed` sans règle). L'UI permet d'éditer
chaque valeur immédiatement. Alternative écartée : bibliothèque sans règles
(source d'erreurs silencieuses).

## 2026-07-12 — MP4 : attachments exclus, sous-titres texte en mov_text

MP4 ne transporte ni attachments Matroska ni SRT natif. En conteneur MP4, le
builder ajoute `-map -0:t` et `-c:s mov_text` (+ `+faststart`). Les sous-titres
image (PGS/VobSub) feront échouer le job avec l'erreur FFmpeg visible dans le
log — comportement volontairement explicite plutôt que perte silencieuse.
MKV reste le défaut (§5.3).

## 2026-07-12 — Image docker : python:3.12-slim-bookworm

Debian 12 fournit Python 3.11 or la stack impose 3.12 (§3). Base
`python:3.12-slim-bookworm` (Debian bookworm) + dépôt jellyfin
(jellyfin-ffmpeg7) + composants non-free (intel-media-va-driver-non-free) +
mesa-vulkan-drivers (ANV pour libplacebo/P5) + mkvtoolnix + dovi_tool (binaire
release épinglé). L'app tourne en `PUID:PGID` via `user:` du compose.

## 2026-07-12 — Estimation de gain pour l'ordonnancement §5.2

« Plus gros gain potentiel estimé » implémenté par une colonne calculée au
scan : `estimated_gain_bytes = max(0, (bitrate_source − bitrate_cible) ×
durée)`. Persistée pour trier en SQL sans re-calcul par claim. Fichiers sans
bitrate connu : NULLS LAST.

## 2026-07-12 — `queue_paused`/`paused` : ajouts de colonnes en migration 0003

La pause par bibliothèque (§5.2) nécessitait un état distinct de `enabled`
(qui gouverne aussi le scan) : colonne `libraries.paused`. Le backoff des
retries utilise `media_files.next_attempt_at` (pas de re-tentative avant).

## 2026-07-12 — shadcn/ui : composants locaux sur primitives Radix

Le générateur shadcn n'est pas utilisé (pas de `components.json`) : les
composants (`button`, `card`, `badge`, `tabs`, `dialog`, `switch`, …) sont
écrits dans `web/src/components/ui/` selon les conventions shadcn/ui exactes
(Tailwind + variables CSS + `cn()` + primitives Radix pour tabs/dialog/switch,
`class-variance-authority`). Même code qu'un `shadcn add`, sans la CLI.

## 2026-07-12 — Vérification visuelle du frontend non exécutée en dev

`tsc --noEmit` strict et le build Vite de production sont verts ; la
navigation du navigateur intégré de l'environnement de dev étant bloquée,
la vérification pixel s'appuie sur la procédure de rodage §9.5 (l'UI est
servie par l'app en conteneur).

## 2026-07-12 — Passe de validation sur environnement réel (PostgreSQL 16 local)

**Contexte.** L'environnement de dev n'avait ni base ni GPU : les chemins DB
n'avaient jamais tourné. Un PostgreSQL 16 local a été monté pour valider
l'intégralité : migrations 0001→0003 depuis une base vierge, seed complet
(3 bibliothèques, 72 règles, GPU), boot lifespan, les 41 endpoints REST,
les 2 WebSockets, le frontend servi en statique, l'export/import de config.

**Corrections issues de cette passe :**
1. `alembic check` a détecté deux index présents dans les migrations mais
   absents des modèles ORM (`ix_bitrate_rules_library`, `ix_mf_gain`) →
   déclarés dans `models.py`. C'est exactement le rôle de ce check en CI.
2. Gain estimé (§5.2) : les MKV sans `bit_rate` de flux ni tag `BPS`
   (sortie ffmpeg brute, certains remux) donnaient `estimated_gain_bytes =
   NULL` → fallback `effective_video_bitrate_kbps()` approximé depuis
   taille/durée (× 0,85 pour la part audio/conteneur). Le bitrate affiché
   reste honnête (NULL), seule l'estimation d'ordonnancement l'utilise.
3. Nouveau test d'intégration `test_pipeline_db.py` (skip sans PG/ffmpeg) :
   claim `FOR UPDATE SKIP LOCKED` → encodage (builder monkeypatché en
   libx265, même squelette) → 9 validations → REMPLACEMENT ATOMIQUE réel
   (corbeille, mtime restauré, tag) → rescan sans re-queue (§5.7).
   Avec base : 297/297 tests, zéro skip.

## 2026-07-12 — Fallback SPA pour les liens profonds (bug trouvé en test visuel)

`StaticFiles(html=True)` ne sert `index.html` que pour `/` : naviguer
directement vers `/queue` ou rafraîchir la page renvoyait le 404 JSON de
l'API. Correctif : `SpaStaticFiles` (main.py) — toute route inconnue hors
`/api`/`/ws` (matchées avant le mount) sert `index.html`, le routeur React
prend le relais. Les six écrans ont été vérifiés visuellement contre le
backend réel (navigateur intégré), données live comprises.

## 2026-07-13 — Fix build Docker : conflit Signed-By sur les sources Debian

**Problème (remonté au déploiement réel).** `docker compose up -d --build`
échouait sur la couche apt (exit 100) : `E: Conflicting values set for
option Signed-By regarding source http://deb.debian.org/debian/ bookworm`
puis `E: The list of sources could not be read.` L'image
`python:3.12-slim-bookworm` déclare les dépôts Debian au format deb822
(`/etc/apt/sources.list.d/debian.sources`) AVEC `Signed-By:` explicite ;
ajouter une seconde source one-line (`non-free.list`) sur le MÊME dépôt
sans `Signed-By` est un conflit fatal pour apt.

**Correctif (reproduit et validé en local, build amd64 complet).** Activer
non-free/non-free-firmware en éditant `Components:` du fichier deb822
existant (sed), avec repli sur l'ancienne méthode one-line si le fichier
deb822 n'existe pas (image sans deb822). Au passage, les runtimes QSV
(`libvpl2`, `libmfx-gen1.2`, `libmfx1`, `intel-opencl-icd`) sont installés
en mode tolérant : tous existent en bookworm, mais jellyfin-ffmpeg embarque
ses propres bibliothèques oneVPL — une évolution de Debian ne doit pas
casser le build.

**Second correctif révélé par le build complet.** L'archive de release de
dovi_tool contient un unique membre nommé `./dovi_tool` (préfixe `./`) :
`tar -x … dovi_tool` échouait (« Not found in archive », exit 2) →
extraction sans nom de membre + `test -x` de contrôle. Les tests e2e
cherchent désormais ffmpeg dans le PATH PUIS dans /usr/lib/jellyfin-ffmpeg :
`docker compose exec app pytest` exécute la e2e complète dans le conteneur
(296 verts + 2 skips DB en local, 298 attendus sur le serveur).

**Validation.** Image buildée en linux/amd64 : jellyfin-ffmpeg 7.1.4 avec
hevc_qsv/av1_qsv/hevc_vaapi/av1_vaapi compilés, libplacebo AVEC
apply_dolbyvision (prérequis DV P5), dovi_tool 2.1.2, mkvpropedit 74,
frontend servi, suite pytest verte dans le conteneur.

## 2026-07-13 — .dockerignore : le node_modules de l'hôte cassait le build web

**Problème (déploiement réel, 2e itération).** `RUN npm run build` échouait
avec `sh: 1: tsc: Permission denied` : sans `.dockerignore`, `COPY web/ ./`
embarquait le `web/node_modules` présent sur l'hôte (copié entre machines →
bits d'exécution/symlinks perdus) et ÉCRASAIT le node_modules propre installé
par `npm ci` dans le stage webbuild.

**Correctif.** `.dockerignore` à la racine : `web/node_modules`, `web/dist`,
`data/`, `.venv`, caches Python, `.env`, `.git`. Effet vérifié : contexte de
build réduit à ~5 Kio, stage webbuild rebuildé de zéro → `tsc --noEmit` +
`vite build` verts, image complète OK.

## 2026-07-14 — Observabilité des jobs + relance des jobs figés/dry-run

Retours d'exploitation réelle : impossible de savoir où en est un job
(« freeze à 0 % pendant tout l'encodage », validation muette pendant de
longues minutes — le décodage d'intégrité CPU d'un gros fichier dure
aussi longtemps que l'encodage), et pas de moyen de relancer les jobs
figés par un arrêt brutal ni de re-traiter les fichiers finis en dry-run.

- Logs docker détaillés : démarrage de job (fichier, codec→codec, action,
  worker, dry-run), progression d'encodage toutes les 30 s (%, fps, x,
  ETA), chaque contrôle de validation annoncé (« validation 5/9 intégrité »),
  progression du décodage d'intégrité en %, issue finale (succès/gain/
  corbeille) ;
- Phase visible dans l'UI : champ `phase` sur les jobs actifs (encodage /
  injection HDR / validation x/9 / remplacement), événement WS `phase`,
  affiché sur les cartes du Dashboard — la barre avance aussi pendant la
  validation (progression du décodage) ;
- `POST /queue/recover` : remet en queue les jobs figés en running/
  validating/replacing qui ne tournent pas réellement dans le process
  (bouton « Relancer les jobs bloqués », onglet Queue) ;
- `POST /files/requeue-dry-run[?library_id=]` : re-queue les fichiers done
  dont le dernier job réussi était un dry-run (bouton « Re-queuer les
  dry-run ») — sans attendre le prochain scan.

Note environnement : le repo vit désormais sous iCloud Drive (« Mobile
Documents » contient un espace) → les faux binaires des tests utilisent
un wrapper /bin/sh au lieu d'un shebang python (les shebangs ne supportent
pas les espaces). Historique git réinitialisé après un déplacement de
dossier sans fichiers cachés ; remote : github.com/literalynn/transcodarr.

## 2026-07-14 — Abandon anticipé, monitoring ressources, mobile, i18n FR/EN

- **Abandon anticipé « gain insuffisant »** (façon Tdarr) : dès 15 %
  d'encodage, si la taille projetée (`total_size` de `-progress` ÷ avancement)
  dépasse le seuil de gain de la bibliothèque (+5 % de marge anti-bruit VBR),
  l'encodage est interrompu → job/fichier `skipped_no_gain`, tmp supprimé,
  original intact. Évite de finir un 4K DV de 2 h pour rien. Réglage
  `early_abort_no_gain` (défaut : activé), désactivable dans l'UI.
- **Monitoring ressources** : `GET /stats/resources` sépare l'app (cgroup v2
  du conteneur : cpu.stat/memory.current) du reste du système (/proc/stat,
  /proc/meminfo qui exposent l'HÔTE) ; GPU best-effort via intel_gpu_top -J
  (paquet ajouté à l'image + CAP_PERFMON dans le compose), repli fréquences
  sysfs, null si indisponible. Carte Dashboard avec barres empilées
  app/système, rafraîchie toutes les 5 s.
- **Mobile** : sidebar → drawer avec hamburger sous md, grilles en 1 colonne,
  tables à défilement horizontal, recherche pleine largeur. Vérifié au
  viewport 375×812.
- **i18n** : français = clé, dictionnaire EN (`web/src/lib/i18n.tsx`),
  bascule FR/EN persistée (localStorage) dans le pied de la sidebar. Les
  phases envoyées par le backend (français) sont traduites côté client par
  table de préfixes. App renommée « Transcodarr » (UI + titre d'onglet).

## 2026-07-14 — Préparation au partage public (v0.9.0)

- **GPU du dashboard réécrit** : intel_gpu_top exige des capacités root
  effectives (un process non-root ne les hérite pas, même avec cap_add) →
  lecture des compteurs DRM `/proc/<pid>/fdinfo` (drm-engine-*) des
  processus du conteneur. Mesure fiable, sans privilège, de l'usage GPU
  de l'app (moteur le plus occupé, video pour QSV). RAM affichée en
  barre simple utilisé/total (le split app/système restait confus).
- **Version affichée** : fichier VERSION (repo) + BUILD_DATE (posé au
  build de l'image), exposés par /health, affichés dans la sidebar à la
  place de l'identifiant de tag interne (jftranscoder-v1 reste le tag
  anti-boucle : NE PAS le changer, tout re-queuerait).
- **Stats par bucket réel** : gain-by-bucket regroupe par bucket de
  hauteur (1920x804 → 1080p) au lieu de la résolution brute — fini les
  groupes d'un seul fichier par crop cinémascope.
- **Anglais par défaut**, sélecteur de langue dans les réglages (le
  bouton de la sidebar disparaît). Chaînes personnelles neutralisées.
  Grille des bitrates scrollable sur mobile. Favicon SVG. README réécrit
  en anglais générique pour publication GitHub.

## 2026-09-14 — Fiabilisation (0.10.0) : analyse des échecs de production, pré-vol, replis, UI unifiée

**Constat (données réelles de l'instance, 106 319 jobs).** Taux d'échec
affiché 45,8 %, mais seulement **436 fichiers** derrière les 48 698 échecs :

1. **Boucle de re-tentatives infinie** (cause majeure du taux). Sur échec,
   `applied_rule_hash` n'était jamais écrit ; le scan voyait « règle
   changée » (None ≠ hash), remettait `retry_count` à 0 et relançait.
   Jusqu'à 174 tentatives pour le même épisode (3 par jour depuis juillet),
   ~825 h de calcul perdues sur l'échantillon importé.
2. **Codes de sortie 218/234** : AVERROR tronquées sur 8 bits (256−38 =
   ENOSYS, 256−22 = EINVAL). Logs : `Subtitle codec 94213 is not supported`
   (mov_text des MP4 copié en MKV) et `Only audio, video, and subtitles are
   supported for Matroska` (piste data tmcd). Reproduits à l'identique en
   local, ainsi qu'un cas latent : audio `pcm_bluray`/`pcm_dvd` (m2ts/VOB).
3. **Contrôle d'intégrité faux positif** : il décodait TOUTES les pistes,
   donc l'audio copié bit à bit dont les trames abîmées préexistaient dans
   la source (`[ac3] expacc out-of-range`, `[mp3float] Header missing`) ou
   le bruit du muxer null (`non monotonically increasing dts`).
4. **Contrôle de durée faux positif** : durées CONTENEUR comparées. Une
   durée Matroska allongée par un sous-titre (événement ASS prolongé) ou
   estimée depuis le débit audio (AVI DivX) échouait alors que les pistes
   étaient identiques (Δ jusqu'à 3 606 s sur des épisodes de 24 min).
5. **« 5_integrity : erreurs : » sans message** (20 fichiers, 217 jobs) :
   code ≠ 0 sans stderr, cause non confirmable sans accès au serveur —
   profil d'un décodeur qui s'arrête sur signal. Diagnostic désormais
   enregistré (code, signal) plutôt que deviné.
6. **Encodages sans gain prévisibles** : sur ~700 jobs échantillonnés,
   293/297 « gain insuffisant » avaient un bitrate vidéo source < cible,
   contre 2/392 succès (tous deux à 0,98×cible).

**Décisions.**
- **Échec définitif = définitif** : le hash de la règle est mémorisé sur
  échec ; le scan ne relance un `failed` que si le fichier (taille/mtime)
  ou la règle change, ou pour les échecs levés par le scan lui-même
  (sonde, règle absente). Les échecs transitoires restent `queued` avec
  backoff et ne deviennent `failed` qu'une fois épuisés.
- **Plan de pistes** (`core/stream_plan.py`) décidé avant l'encodage et
  traduit en mapping EXPLICITE (vidéo d'abord, `-c:N` par piste de sortie) :
  mov_text & co → SubRip, PCM Blu-ray/DVD → FLAC (sans perte), data et
  pièces jointes sans type MIME exclues, motif enregistré par piste.
  L'attendu du contrôle 6 vient du plan. Plus de `-map 0 -c copy` aveugle.
- **Classification des échecs** (`core/failures.py`) : catégorie stable
  (TEXT, pas d'ENUM PG) + lignes d'erreur filtrées (bruit libva retiré).
  Historique reclassé en tâche de fond au démarrage, numéros de tentative
  reconstitués par la migration 0004.
- **Stratégies de repli cumulatives** (`core/strategy.py`) : standard →
  tolérant (`+genpts+discardcorrupt`, `-err_detect ignore_err`,
  `-max_error_rate 1`) → pistes strictes (liste blanche) → autre API GPU
  (QSV ↔ VAAPI, testée au démarrage). La catégorie choisit le repli ;
  une stratégie déjà tentée ou de niveau inférieur n'est jamais rejouée ;
  garde-fou de 6 tentatives par série. Toutes restent soumises aux mêmes
  validations bloquantes.
- **Pré-vol** (`core/preflight.py`) : source lisible et stable (modifiée
  depuis < 120 s = copie en cours, retentée), sonde, gain atteignable,
  test de décodage vidéo de 3 × 3 s (erreurs → départ direct en tolérant ;
  artefacts normaux après seek sur GOP ouvert filtrés). La prédiction de
  gain est optimiste (sortie à 80 % de la cible, 10e centile mesuré) et
  fondée sur le RAPPORT des bitrates : une durée conteneur fausse ne peut
  pas faire écarter un fichier. Bitrate seulement estimable : pas d'avis
  au scan, durée réelle mesurée au pré-vol. « Forcer » contourne.
- **Validation** : intégrité = vidéo seule (seule piste réencodée) avec
  seconde passe mono-thread si le décodeur s'arrête sans message, délai
  proportionnel à la durée ; durée = second avis sur les horodatages réels
  des paquets (vidéo + 1re piste audio) quand les durées conteneur
  divergent, lecture de fin de fichier (quelques ms), parcours complet
  seulement si nécessaire.
- **UI** : Dashboard et Statistiques fusionnés (période 24 h / 7 j / 30 j /
  tout, taux de réussite, réussite au 1er essai, causes d'échec avec
  relance par cause) ; Historique intégré à la Queue (onglets En attente /
  En cours / Historique filtrable / Échecs par fichier avec sélection et
  relance) ; fenêtre de diagnostic commune (cause en clair, remède,
  décision automatique, lignes d'erreur, pré-vol, plan de pistes,
  validation, chronologie des tentatives). `/history` et `/stats` redirigés.
  Palette des graphiques validée (daltonisme/contraste) dans les deux thèmes.

**Vérifications.** 383 tests verts contre PostgreSQL 16 (dont 4 scénarios
de bout en bout rejouant les échecs de production : MP4 mov_text+data réussi
au 1er essai, repli qui récupère, échec définitif non relancé par le scan,
écartement sans encodage), mypy strict, ruff, build Vite. UI vérifiée sur un
import local de ~60 000 jobs réels (bureau et mobile, FR/EN). Non vérifiable
hors du serveur : comportement exact de jellyfin-ffmpeg 7.1 + QSV sur les
fichiers réels (les correctifs de mux ont été reproduits avec FFmpeg 9 et
libx265 ; les messages des muxers concernés sont identiques).

## 2026-09-14 — Sous-titres : réencoder sans perte, ne jamais toucher aux ASS

**Règle (demande explicite de l'utilisateur).** Un réencodage ASS casse très
souvent la mise en page des fansubs : **les pistes ASS/SSA ne sont jamais
réencodées ni exclues**, quelle que soit la stratégie. Seuls les formats dont
le réencodage ne perd rien sont reconstruits en repli : SubRip et texte brut
→ SubRip (paquets et horodatages refaits). WebVTT (réglages de position) et
sous-titres image restent copiés. Restent convertis les seuls formats qu'il
FAUT convertir pour conserver la piste en Matroska (mov_text, CEA-608,
microDVD…, télétexte en tentative).

**Mise en œuvre.** Nouvelle stratégie `reencode_subs` entre `tolerant` et
`safe_streams` ; une erreur de sous-titres passe par elle AVANT toute
exclusion, et `safe_streams` n'exclut que l'impossible (codec inconnu non
écrivable, jamais un ASS). Un repli n'est tenté que s'il change réellement
la commande pour ce fichier (empreinte du plan de pistes + options
d'entrée) : un fichier dont les sous-titres sont en ASS n'enchaîne plus
d'encodages identiques voués au même échec.

## 2026-09-14 — Tests en production : cinq corrections issues des fichiers réels

Banc d'essai non destructif (sortie hors médias, sans écriture en base) puis
passage réel par la file, sur un fichier de chaque catégorie d'échec.

1. **Sous-titres ASS déplacés par `-analyzeduration/-probesize 200M`** (ajouté
   le 16/07). Sur un MKV, le muxer reçoit alors un paquet de sous-titres
   tardif en premier et « corrige » tous les suivants (`Non-monotonic DTS …
   changing to`) : 5 057 événements empilés à 23:00 sur un épisode d'anime.
   Reproduit et isolé (même 20M suffit ; valeurs par défaut : 0 correction).
   En Matroska/MP4 le codec vient de l'en-tête : sonde agrandie réservée aux
   TS/VOB/AVI. Audit des logs : 28 fichiers remplacés depuis juillet portent
   des corrections (13 épisodes d'une même saison gravement, 5 films 10-40 s, le reste
   ≤ 3 s) ; les 2 originaux encore en corbeille ont été restaurés (encodage
   fautif conservé à côté) et réencodés.
2. **Nouveau contrôle bloquant 6b « minutage des pistes copiées »** : relevé
   des `Non-monotonic DTS` du log d'encodage ; décalage > 250 ms d'une piste
   copiée = échec, original conservé. Il a immédiatement refusé un remux dont
   les blocs PGS sont désordonnés dans la source (669 paquets, 38,9 s) — que
   la 0.9 avait remplacé la veille.
3. **Assemblage mkvmerge** (stratégie `mkvmerge_mux`, repli de 6b et des
   erreurs de mux) : FFmpeg n'encode que la vidéo et les conversions, mkvmerge
   recopie audio/sous-titres/polices/chapitres de la source. Mesuré : timing
   identique à la microseconde près, y compris sur la source désordonnée où
   FFmpeg déplaçait 1 338 paquets. Repli seulement : le chemin FFmpeg reste
   le défaut (exact sur les sources ordonnées, déjà validé).
4. **Intégrité « vidéo seule » encore polluée** : même avec `-map 0:v:0`,
   l'ouverture du fichier décode quelques trames de toutes les pistes ;
   « [mp3float] Header missing » d'une piste copiée faisait échouer ~170
   fichiers. Filtrage par composant FFmpeg (audio, sous-titres, muxer null).
5. **Durée réelle des AVI** : après un seek, le demuxer AVI recalcule les
   horodatages depuis l'index de l'en-tête (1 485 s au lieu de 1 471,5 s en
   lecture séquentielle) → parcours séquentiel pour les conteneurs aux
   horodatages approximatifs. Aussi : « first frame is no keyframe » (VC-1
   après seek) classé normal ; la config de logs d'Alembic coupait tous les
   logs applicatifs après la migration (`disable_existing_loggers`).

6. **Garde anti-upgrade : référence = date relevée au démarrage du job**, plus
   la date du dernier scan. Constat : un service externe au transcodeur
   retouche chaque fichier remplacé ~1 min après (mtime et ctime ; la
   restauration de date par Transcodarr est vérifiée correcte sur le NFS hors
   bibliothèque) ; tout fichier retouché entre scan et job était annulé à tort
   (« source modifiée pendant l'encodage »), encodage perdu.

Résultats réels : les 7 catégories d'échec sélectionnées passent (gains 11 à
74 %) ou sont écartées à juste titre avant encodage (gain prédit −24 %,
mesuré −25 %) ; 0 échec sur 8 fichiers relancés par la file.

## 2026-09-15 — Revue du code 0.10.0 et dashboard : périodes partout, donut des codecs

**Période des statistiques.** Le sélecteur 24 h / 7 j / 30 j / Tout filtre
désormais toutes les statistiques d'activité : tentatives, causes d'échec,
espace récupéré, débit d'encodage et gain par bucket (KPI compris). Les
cartes d'état (codecs, progression, fichiers restants, ressources) décrivent
la bibliothèque actuelle ; le titre des cartes filtrées rappelle la période.
- Périodes **alignées** : 24 créneaux horaires pour 24 h (heure en cours
  comprise), sinon N jours calendaires UTC. Mêmes bornes pour toutes les
  cartes : les totaux concordent d'une carte à l'autre.
- Courbes renvoyées **avec tous les créneaux**, vides compris (axe du temps
  régulier) ; granularité heure (24 h ou historique ≤ 2 jours), jour, semaine
  au-delà de 3 mois. Créneaux calculés en UTC côté SQL
  (`date_trunc(timezone('UTC', …))`) quel que soit le fuseau de la session.
- Endpoints `savings-over-time` et `throughput` : enveloppe `{unit, points,
  totaux}` au lieu d'une liste ; `reliability.per_day` → `timeline`.

**Répartition des codecs : donut** (retour utilisateur : plus lisible que les
barres). Couleur attachée au codec, jamais au rang : HEVC, AV1, H.264 dans
l'ordre des séries validé (bleu, orange, aqua — paires distinguables en
daltonisme, thèmes clair et sombre) ; le reste regroupé en « Autres » gris,
détaillé dans la légende. Légende avec pourcentages et effectifs à côté du
cercle (l'aqua clair est sous 3:1 : l'étiquette directe est obligatoire).

**Corrections issues de la revue :**
- ETA de la file multipliée par le nombre de workers (durée moyenne × fichiers
  en file, sans tenir compte du parallélisme) ; moyenne désormais sur les 50
  derniers encodages, indépendante des périodes d'inactivité.
- Contrôle 6b : un job repris après crash réutilise son log ; seules les
  lignes de la tentative en cours sont lues (décalage mémorisé au pré-vol),
  sinon les corrections d'un essai interrompu faisaient échouer le suivant.
- Intermédiaire mkvmerge supprimé aussi en cas d'erreur inattendue.
- Scan : un fichier « sans gain » réévalué après changement de règle mémorise
  la nouvelle règle (plus de réévaluation à chaque scan) ; un fichier en file
  au milieu d'une série de replis garde sa dernière erreur affichée.
- Cache HTTP de l'interface : `index.html` partait sans en-tête de cache et
  le navigateur réaffichait l'ancienne interface après une mise à jour
  (constaté au déploiement). `index.html` est désormais toujours revalidé,
  les assets Vite hachés sont immuables, et un asset absent renvoie un vrai
  404 au lieu d'index.html.
