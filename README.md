# Transcodeur Jellyfin — remplacement de Tdarr

Application web self-hosted d'optimisation automatique de bibliothèques
Jellyfin : réencodage HEVC/AV1 sur GPU Intel (QSV/VAAPI), gestion complète
du HDR10/HDR10+/HLG/**Dolby Vision**, validation post-encode en 9 contrôles
bloquants, remplacement atomique avec corbeille — **jamais destructeur**.

```
/opt/transcoder/
├── docker-compose.yml       # un seul fichier, deux services (app + db)
├── .env                     # SEUL fichier d'infra à éditer (cp .env.example .env)
├── .env.example
├── Dockerfile               # jellyfin-ffmpeg + drivers Intel + Vulkan + dovi_tool
├── app/                     # backend FastAPI (Python 3.12)
├── web/                     # frontend React 18 (servi par l'app)
├── scripts/
│   ├── check_gpu.sh         # diagnostic GPU hôte ET conteneur
│   ├── make_sample_set.sh   # échantillon de rodage (§ Rodage)
│   ├── reset.sh             # remise à zéro totale
│   ├── backup.sh            # pg_dump + export JSON en une commande
│   └── restore.sh
└── data/                    # TOUT l'état persistant, bind-mount → sauvegarde par tar
    ├── postgres/            # base (config, historique, états)
    ├── logs/                # logs FFmpeg par job
    ├── tmp/                 # temporaire optionnel (voir « Dossier temporaire »)
    ├── trash/               # corbeille des originaux remplacés
    └── out/                 # sorties du mode dry-run
```

---

## Installation et rodage (pas-à-pas)

### 1. Prérequis Debian pour l'Arc A770

```bash
uname -r                                   # kernel >= 6.2 requis (sinon backports)
sudo apt install firmware-misc-nonfree intel-media-va-driver-non-free vainfo
ls -l /dev/dri/                            # renderD128 doit exister
vainfo --display drm --device /dev/dri/renderD128   # HEVC ET AV1 en Enc
getent group render | cut -d: -f3          # → RENDER_GID pour le .env
```

Tout est automatisé et diagnostiqué par :

```bash
./scripts/check_gpu.sh              # sur l'hôte
./scripts/check_gpu.sh --container  # DANS le conteneur (après docker compose up)
```

### 2. Déploiement

```bash
sudo mkdir -p /opt/transcoder && cd /opt/transcoder
git clone <repo> . # (ou dézippage)
cp .env.example .env
$EDITOR .env       # MEDIA_ROOT, RENDER_GID, PUID/PGID, mot de passe DB
```

`PUID`/`PGID` doivent correspondre au propriétaire des fichiers médias
(l'app restaure uid/gid/permissions à l'identique après remplacement).

### 3. Premier démarrage

```bash
docker compose up -d --build
```

Migrations + seed automatiques : bibliothèques `Films`/`Séries`/`Animés`,
les **72 règles de bitrate** du cahier des charges (HEVC + AV1 × 6 buckets ×
SDR/HDR), le GPU Arc A770 sur `renderD128`. `DRY_RUN=true` par défaut :
**aucun original ne peut être remplacé** tant que vous ne l'avez pas
explicitement désactivé. UI : `http://serveur:8080`.

### 4. Tester le GPU — ne rien faire d'autre avant

UI → **GPU & Réglages** → **Tester le GPU** : encode 5 s de mire en HEVC
**et** AV1 sur le node configuré et affiche le résultat par encodeur.
Le démarrage teste aussi automatiquement libplacebo (option
`apply_dolbyvision`, indispensable au DV P5) et la chaîne zéro-copie
Vulkan→QSV ; le résultat est visible sur le Dashboard et via
`GET /api/v1/capabilities`. En cas d'échec QSV, le fallback VAAPI est
automatique et loggé.

### 5. Échantillon de rodage

```bash
./scripts/make_sample_set.sh --dv --hdr --already-hevc -n 5
```

Copie (reflink si possible, mtime préservés) de vrais fichiers de votre
collection vers `${SANDBOX_ROOT}/{Films,Séries,Animés}`, avec récapitulatif
codecs/résolutions/HDR/DV. Options : `--hdr --dv --4k --h264 --already-hevc
--random`, cumulables.

### 6. Bibliothèques sandbox

Dans l'UI → **Bibliothèques** → Ajouter : 3 bibliothèques pointant sur
`${SANDBOX_ROOT}/…` avec vos vrais réglages. Lancer un scan, observer la
queue et les logs FFmpeg en direct (Dashboard → « Voir les logs »).

### 7. Tableau de validation attendu

| Cas de test | Statut attendu | Sortie attendue | Vérification `ffprobe` |
|---|---|---|---|
| H.264 SDR 1080p | `done`, job `success` | HEVC (ou AV1) au bitrate de la règle 1080p SDR, audio/ST/chapitres intacts | `ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,bit_rate -of default=nw=1 f.mkv` |
| HEVC déjà conforme (tag présent) | `done` **sans nouveau job** au rescan | fichier inchangé | `ffprobe -v error -show_entries format_tags=TRANSCODER_TAG -of default=nw=1 f.mkv` → tag `jftranscoder-v1\|…` |
| HDR10 4K | `done` | flags BT.2020/PQ + MDCV/CLL présents, 10-bit | `ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries,color_transfer,color_space,pix_fmt -of default=nw=1 f.mkv` puis `ffprobe -v error -select_streams v:0 -show_frames -read_intervals "%+#1" f.mkv \| grep -A12 "Mastering"` |
| DV Profil 5 | `done` (si libplacebo DV OK) **ou** `skipped_policy` motif « conversion de gamut indisponible » | HDR10 valide (BT.2020/PQ, MDCV/CLL), **plus aucun** DOVI ; jamais d'image verte/violette (contrôle 9 bloquant) | `ffprobe -v error -select_streams v:0 -show_frames -read_intervals "%+#1" f.mkv \| grep -c DOVI` → **0** |
| DV Profil 8.1 | `done` | HDR10 (BL réencodée sans RPU), pas de DOVI résiduel | idem ci-dessus + flags couleur BT.2020/PQ |
| Multi-pistes (audio/ST/chapitres/polices) | `done` | mêmes NOMBRES de pistes audio et sous-titres, chapitres et attachments conservés (MKV) | `ffprobe -v error -show_entries stream=index,codec_type -of csv f.mkv \| sort \| uniq -c` et `ffprobe -v error -show_chapters f.mkv` |
| Fichier corrompu | `failed`, motif explicite (sonde ou contrôle 5), original intact | aucun remplacement | l'original est toujours là, `data/logs/job_*.log` contient l'erreur |
| Fichier hors filtre de date | `ignored`, motif « antérieur au filtre de date » | jamais mis en queue | visible dans l'UI → Fichiers |

### 8. Passage en réel (toujours sur la sandbox)

UI → Bibliothèque sandbox → désactiver le dry-run (surcharge par
bibliothèque) **ou** GPU & Réglages → `dry_run_global = false`. Vérifier
après un job réussi :

- remplacement atomique : le fichier au même chemin, lisible immédiatement ;
- corbeille : l'original dans `data/trash/<date>-job<id>/` ;
- `mtime` et permissions préservés : `stat fichier` avant/après identiques ;
- **rollback** : Historique → job → `trashed_path`, puis
  ```bash
  # supprimer/déplacer la version transcodée, puis :
  docker compose exec app python -c "
  from transcoder.core.replace import restore_from_trash
  restore_from_trash('/data/trash/<...>/film.mkv', '<chemin original>')"
  ```
  (ou simplement `mv` depuis `data/trash/` — c'est un fichier ordinaire).

### 9. Bascule production

Créer les vraies bibliothèques (`/srv/storage/Films`, `/srv/storage/Séries`,
`/srv/storage/Animés`) — **1 worker**, corbeille activée
(`immediate_delete = false`, défaut). Contrôler manuellement les 3 premiers
fichiers traités (tableau du point 7), puis monter `worker_count`.
Grâce au montage `${MEDIA_ROOT}:${MEDIA_ROOT}`, ajouter/supprimer une
bibliothèque sous `MEDIA_ROOT` ne demande **aucune** modification du compose.

### 10. Sauvegarde / restauration — pour ne jamais reperdre la config

```bash
./scripts/backup.sh                                    # pg_dump + export JSON → backups/
./scripts/restore.sh backups/transcoder-backup-X.tar.gz            # base complète
./scripts/restore.sh backups/transcoder-backup-X.tar.gz --config   # config JSON seule
```

L'export/import JSON est aussi disponible dans l'UI (GPU & Réglages) et via
`GET /api/v1/config/export` / `POST /api/v1/config/import`. Et comme tout
l'état vit dans `data/` (bind-mounts), `tar czf backup.tar.gz data/` (app
arrêtée) sauvegarde l'intégralité.

### 11. Remise à zéro totale

```bash
./scripts/reset.sh    # conteneurs + data/ + sandbox (confirmation requise)
```

---

## Notes d'exploitation

### « Date d'ajout » : approximation et limites

Le filtre « ne traiter que les fichiers ajoutés après le … » utilise, dans
l'ordre : **`DateCreated` de Jellyfin** (si URL + API key renseignées dans
les réglages — le plus fiable), sinon **btime** (date de création du fichier,
quand le filesystem l'expose), sinon **`max(mtime, ctime)`**. ⚠️ Après une
copie de fichiers (`cp`, `rsync` sans `-t`), mtime/ctime/btime peuvent
refléter la copie et non l'ajout réel : configurez l'intégration Jellyfin
dans ce cas. Les fichiers hors filtre passent en `ignored` (réversible).

### Dossier temporaire (§9.3)

Défaut `tmp_strategy = sibling` : encodage dans
`<dossier_du_fichier>/.transcoder_tmp/` → même filesystem, `rename()`
atomique garanti. `data_tmp` n'est utilisé que si `data/tmp` est sur le
**même `st_dev`** que le fichier cible (vérifié à chaque job) ; sinon bascule
automatique sur le sibling — jamais de copie inter-filesystem non atomique.

### Conteneur MP4 (option)

MKV est le défaut et le seul conteneur sans perte. En MP4 : les attachments
(polices) sont exclus, les sous-titres texte convertis en `mov_text`, les
sous-titres image (PGS/VobSub) font échouer le job avec une erreur explicite.
En HDR, si la source ne porte pas de MDCV/CLL, l'injection conteneur
(mécanisme Matroska) n'existe pas en MP4 → validation en échec, original
conservé.

### Options FFmpeg vérifiées au démarrage (et comment les vérifier soi-même)

Ces options dépendent de la version de jellyfin-ffmpeg embarquée ; elles sont
**testées au démarrage** par `capabilities.py` (encodage de mire réel) et
exposées via `GET /api/v1/capabilities` :

| Option/chaîne | Vérification manuelle |
|---|---|
| `hevc_qsv`, `av1_qsv`, `hevc_vaapi`, `av1_vaapi` | `/usr/lib/jellyfin-ffmpeg/ffmpeg -h encoder=av1_qsv` |
| `-preset slow` (QSV) | idem (`ffmpeg -h encoder=hevc_qsv`) |
| `libplacebo` + option `apply_dolbyvision` (**bloquant pour le DV P5**) | `/usr/lib/jellyfin-ffmpeg/ffmpeg -h filter=libplacebo \| grep apply_dolbyvision` |
| Chaîne zéro-copie `libplacebo …,hwmap=derive_device=qsv,format=qsv` | testée au boot ; en échec → repli logiciel (libplacebo → RAM → QSV), loggé |
| `hwupload` VAAPI | `ffmpeg -h filter=hwupload` |

Sans libplacebo DV : **aucun** fichier P5 n'est traité (`skipped_policy`,
motif « DV P5 : conversion de gamut indisponible ») — jamais de best-effort.

### Tests automatisés

```bash
docker compose exec app pytest -v      # unitaires + e2e logicielle + alembic check
```

- unitaires (sans GPU ni ffmpeg) : builder FFmpeg (matrice codec × HDR ×
  profil DV 5/7/8.1/8.2/8.4 × API), matrice DV, buckets, règles/hash
  anti-boucle, validation, remplacement (fs réel), scanner, runner (faux
  ffmpeg), capabilities (faux binaires) ;
- e2e : mire générée puis réencodée en **encodeurs logiciels** (libx264 →
  libx265), validation complète, tag anti-boucle relu, corruption détectée,
  no-gain, annulation SIGTERM — skip propre si ffmpeg absent ;
- `alembic check` : modèles ↔ DDL (skip si PostgreSQL injoignable).

En dev local : `cd app && pip install -e .[dev] && pytest && mypy transcoder` ;
frontend : `cd web && npm ci && npm run build` (inclut `tsc --noEmit` strict).

---

## Conformité §11 (critères d'acceptation)

| Critère | Où / comment il est garanti |
|---|---|
| **Aucun chemin de code ne peut détruire/écraser un original sans validations passées** | L'UNIQUE appel à `replace_original()` (`app/transcoder/core/queue.py`, `_finalize`) est derrière `outcome is PASSED` des 9 contrôles (`core/validate.py`, arrêt au premier échec). `replace.py` ne contient aucun `rm` d'original : corbeille (`_move_to_trash`) ou rename atomique. Échec de validation → suppression du TEMPORAIRE uniquement. Tests : `test_replace.py`, `test_validate.py`, e2e corruption. |
| **`docker compose down` brutal : aucun fichier corrompu, reprise propre** | L'encodage écrit dans `.transcoder_tmp/` (jamais le chemin final) ; original intact jusqu'au rename post-validation. Au boot : `recover_after_crash()` remet `running/validating/replacing → queued` et `cleanup_orphan_tmp()` purge les temporaires (lifespan, `main.py`). `fsync` fichier + répertoires autour du rename. |
| **Un fichier traité n'est jamais re-queué tant que la config ne change pas** | Marqueur anti-boucle §5.7 : tag conteneur `TRANSCODER_TAG` + `applied_rule_hash` en base ; `decide_status()` (`core/scanner.py`) ne re-queue que si tag absent OU hash différent. Le hash inclut codec, bitrates, conteneur, politiques. Tests : `test_scanner.py`, `test_rules.py`, e2e tag. |
| **Métadonnées HDR10 présentes à l'identique en sortie (vérifié ffprobe)** | Passthrough encodeur (side data source) + injection conteneur mkvpropedit (source > RPU L6 > défauts sûrs loggés). Contrôle 8 **bloquant** : flags BT.2020/PQ ou HLG, 10-bit, MDCV+CLL présents, vérifiés par ffprobe sur la sortie (`check_colorimetry`). Tests : `test_validate.py`, `test_replace.py::test_mkvpropedit_args_defaults`. |
| **Tout DV ressort en HDR10 valide, ou n'est pas traité. Aucun gamut cassé possible** | Matrice §5.3.1 implémentée telle quelle (`core/dovi.py`) : P5 ⇒ libplacebo `apply_dolbyvision` obligatoire, sinon `skipped_policy` (capacité vérifiée au boot) ; P4/inconnus ⇒ `skipped_policy` ; 8.2 ⇒ SDR ; 8.4 ⇒ HLG jamais tagué PQ. Contrôle 9 anti-vert/violet **bloquant** (SSIM + delta chroma U/V, référence selon profil). Contrôle 8b : zéro side data DOVI en sortie. Tests : `test_dovi.py` (matrice complète), `test_cmd_builder.py`, `test_validate.py`. |
| **Config modifiable depuis l'UI, exportable, réimportable** | Tout vit en base (`app_settings`, `libraries`, `bitrate_rules`, `gpus`), rechargé à chaud (`runtime_config.py` relu à chaque cycle worker/scan). UI complète (§7.5/7.6). Export/import JSON : UI + `scripts/backup.sh`/`restore.sh` + API. |
| **Un seul dossier, un seul `docker compose up -d`, état dans `data/` sauvegardable au tar** | Arborescence §9.1 respectée ; `data/` en bind-mounts exclusivement (postgres, logs, trash, tmp, out) ; aucun volume nommé. |
| **DRY_RUN global et par bibliothèque, aucun original touché, historique/stats complets** | `dry_run_global` (seed depuis `.env`, défaut **true**) surchargé par `libraries.dry_run` ; en dry-run la sortie va dans `data/out/` (arborescence miroir), `replace_original()` n'est jamais appelé, jobs/stats intégralement remplis. Badge « DRY RUN » pleine largeur dans l'UI. |
| **Ajout/suppression de bibliothèque sans toucher au compose** | Montage `${MEDIA_ROOT}:${MEDIA_ROOT}` (mêmes chemins absolus hôte/conteneur) : tout chemin sous `MEDIA_ROOT` est valide des deux côtés, création par l'UI avec validation d'existence côté serveur. |

Décisions d'implémentation détaillées : voir [DECISIONS.md](DECISIONS.md).
